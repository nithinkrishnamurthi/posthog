import { Counter } from 'prom-client'

import { logger } from '~/utils/logger'

import { DlqOutput, OVERFLOW_OUTPUT, OverflowOutput } from '../common/outputs'
import { CircuitBreaker, CircuitBreakerConfig, CircuitOpenError } from '../utils/circuit-breaker'
import { BatchProcessingStep } from './base-batch-pipeline'
import { PipelineResult, dlq, redirect } from './results'

/**
 * Result type for batch retry steps. Each event either succeeded
 * (with a PipelineResult the wrapper passes through as-is) or failed
 * (with metadata for the wrapper to handle via retry/overflow/circuit).
 */
export type BatchRetryStepResult<T, R extends string = never> =
    | { status: 'success'; result: PipelineResult<T, R> }
    | { status: 'failed'; retriable: boolean; reason: string }

/**
 * A batch step that returns per-event success/failure instead of throwing.
 */
export type BatchRetryStep<TIn, TOut, R extends string = never> = (
    inputs: TIn[]
) => Promise<BatchRetryStepResult<TOut, R>[]>

export interface BatchRetryOptions {
    /** Total number of attempts per failed event group. Defaults to 3. */
    maxAttempts?: number
    /** Base sleep between retries in ms. Doubles each attempt. Defaults to 100. */
    retrySleepMs?: number
    /** Maximum sleep between retries in ms. Defaults to 10000. */
    maxRetrySleepMs?: number
    /** Circuit breaker config. If omitted, no circuit breaker is used. */
    circuitBreaker?: CircuitBreakerConfig
}

const overflowCounter = new Counter({
    name: 'ingestion_batch_retry_overflow_total',
    help: 'Events redirected to overflow after exhausting retries',
    labelNames: ['step'],
})

const dlqCounter = new Counter({
    name: 'ingestion_batch_retry_dlq_total',
    help: 'Events sent to DLQ due to non-retriable failures',
    labelNames: ['step'],
})

const circuitOpenCounter = new Counter({
    name: 'ingestion_batch_retry_circuit_open_total',
    help: 'Number of times the circuit breaker tripped open',
    labelNames: ['step'],
})

/**
 * Wraps a batch step with per-event retry, overflow redirection,
 * DLQ routing, and optional circuit breaking.
 *
 * Behavior:
 * 1. If circuit is open, probes with a single input first. If the probe
 *    fails, throws CircuitOpenError immediately (no wasted retries).
 *    If it succeeds, closes the circuit and processes the full batch.
 * 2. Calls the step with all inputs
 * 3. Retries only the failed+retriable inputs up to maxAttempts
 * 4. After exhausting retries, classifies remaining failures:
 *    - Non-retriable → DLQ (event is broken, retrying won't help)
 *    - Retriable, but some events succeeded → overflow (service works,
 *      these events are problematic)
 *    - Retriable, ALL events failed → service is down, throw
 *      CircuitOpenError (consumer holds batch, keeps Kafka connection
 *      alive, retries after backoff)
 */
export function withBatchRetry<TIn, TOut, R extends string = never>(
    step: BatchRetryStep<TIn, TOut, R>,
    options: BatchRetryOptions = {}
): BatchProcessingStep<TIn, TOut, OverflowOutput | DlqOutput | R> {
    const maxAttempts = options.maxAttempts ?? 3
    const baseSleepMs = options.retrySleepMs ?? 100
    const maxSleepMs = options.maxRetrySleepMs ?? 10_000
    const circuitBreaker = options.circuitBreaker ? new CircuitBreaker(options.circuitBreaker) : null
    const stepName = step.name || 'anonymousBatchRetryStep'

    const retryStep: BatchProcessingStep<TIn, TOut, OverflowOutput | DlqOutput | R> = async (
        inputs: TIn[]
    ): Promise<PipelineResult<TOut, OverflowOutput | DlqOutput | R>[]> => {
        if (circuitBreaker?.isOpen()) {
            // Probe with a single event — no retries, just one attempt.
            // If the dependency is still down this fails fast instead of
            // burning through maxAttempts × timeout for the full batch.
            const probeResults = await step(inputs.slice(0, 1))
            if (probeResults[0].status !== 'success') {
                circuitOpenCounter.labels(stepName).inc()
                throw new CircuitOpenError()
            }
            // Probe succeeded — close circuit and fall through to process
            // the full batch normally. The probe event gets processed again
            // (idempotent) but we avoid duplicating the result handling path.
            circuitBreaker.recordSomeSucceeded()
        }

        const finalResults = await processWithRetries(inputs)
        return classifyResults(finalResults)
    }

    async function processWithRetries(inputs: TIn[]): Promise<BatchRetryStepResult<TOut, R>[]> {
        const results: BatchRetryStepResult<TOut, R>[] = Array.from({ length: inputs.length })
        let pendingIndices = inputs.map((_, i) => i)
        let sleepMs = baseSleepMs

        for (let attempt = 0; attempt < maxAttempts && pendingIndices.length > 0; attempt++) {
            const pendingInputs = pendingIndices.map((i) => inputs[i])

            if (attempt > 0) {
                logger.warn('⚠️', `${stepName}_retry`, {
                    attempt: attempt + 1,
                    maxAttempts,
                    pendingCount: pendingInputs.length,
                })
                await new Promise((resolve) => setTimeout(resolve, sleepMs))
                sleepMs = Math.min(sleepMs * 2, maxSleepMs)
            }

            const stepResults = await step(pendingInputs)

            const stillFailingIndices: number[] = []
            for (let j = 0; j < stepResults.length; j++) {
                const originalIndex = pendingIndices[j]
                const result = stepResults[j]

                if (result.status === 'success') {
                    results[originalIndex] = result
                } else if (result.retriable && attempt < maxAttempts - 1) {
                    stillFailingIndices.push(originalIndex)
                } else {
                    results[originalIndex] = result
                }
            }

            pendingIndices = stillFailingIndices
        }

        return results
    }

    function classifyResults(
        results: BatchRetryStepResult<TOut, R>[]
    ): PipelineResult<TOut, OverflowOutput | DlqOutput | R>[] {
        const anySucceeded = results.some((r) => r.status === 'success')
        const retriableFailures = results.filter((r) => r.status === 'failed' && r.retriable)

        if (circuitBreaker) {
            if (anySucceeded) {
                circuitBreaker.recordSomeSucceeded()
            } else if (retriableFailures.length > 0) {
                const tripped = circuitBreaker.recordAllFailed()
                if (tripped) {
                    circuitOpenCounter.labels(stepName).inc()
                }
                throw new CircuitOpenError()
            }
        }

        // Without a circuit breaker, all-fail retriable batches still shouldn't
        // overflow — throw so the consumer doesn't commit offsets.
        if (!anySucceeded && retriableFailures.length > 0 && !circuitBreaker) {
            throw new CircuitOpenError()
        }

        return results.map((result) => {
            if (result.status === 'success') {
                return result.result
            }
            if (!result.retriable) {
                dlqCounter.labels(stepName).inc()
                return dlq(result.reason)
            }
            overflowCounter.labels(stepName).inc()
            return redirect(result.reason, OVERFLOW_OUTPUT)
        })
    }

    Object.defineProperty(retryStep, 'name', { value: stepName })
    return retryStep
}
