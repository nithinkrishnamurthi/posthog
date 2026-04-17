import { logger } from '~/utils/logger'

export interface CircuitBreakerConfig {
    /** Number of consecutive all-fail batches before tripping the circuit. */
    failureThreshold: number
}

/**
 * Error thrown when the circuit breaker is open, signaling the consumer
 * to pause consumption and wait for the service to recover.
 */
export class CircuitOpenError extends Error {
    constructor(message: string = 'Circuit breaker open — service unavailable') {
        super(message)
        this.name = 'CircuitOpenError'
    }
}

/**
 * Circuit breaker for external service dependencies.
 *
 * Tracks consecutive all-fail batches. When the failure threshold is reached,
 * the circuit opens — indicating the service is likely down. The circuit
 * stays open until recordSomeSucceeded() is called (typically after a
 * successful probe). The failure count persists across open/close cycles,
 * so a single failure after recovery re-opens the circuit immediately.
 *
 * Partial failures (some succeed, some fail) always reset the failure count
 * since they indicate the service is operational — the failures are from
 * specific bad events, not a service outage.
 */
export class CircuitBreaker {
    private open: boolean = false
    private consecutiveFailures: number = 0
    private config: CircuitBreakerConfig

    constructor(config: CircuitBreakerConfig) {
        this.config = config
    }

    /**
     * Record a batch where all events failed. If the failure threshold
     * is reached, trips the circuit open.
     *
     * @returns true if the circuit just tripped open
     */
    recordAllFailed(): boolean {
        this.consecutiveFailures++

        if (this.consecutiveFailures >= this.config.failureThreshold) {
            this.open = true
            logger.warn('⚠️', 'circuit_breaker_opened', {
                consecutiveFailures: this.consecutiveFailures,
                failureThreshold: this.config.failureThreshold,
            })
            return true
        }

        return false
    }

    /**
     * Record a batch where at least some events succeeded. Resets the
     * failure count and closes the circuit.
     */
    recordSomeSucceeded(): void {
        const wasOpen = this.open
        this.consecutiveFailures = 0
        this.open = false

        if (wasOpen) {
            logger.info('✅', 'circuit_breaker_closed', {
                message: 'Service recovered',
            })
        }
    }

    /** Current state for observability. */
    getState(): string {
        return this.open ? 'open' : 'closed'
    }

    /** Whether the circuit is currently open. */
    isOpen(): boolean {
        return this.open
    }
}
