import { useActions, useValues } from 'kea'
import { useEffect, useState } from 'react'

import { IconTrash } from '@posthog/icons'
import { LemonSelect } from '@posthog/lemon-ui'

import api from 'lib/api'
import { LemonButton } from 'lib/lemon-ui/LemonButton'
import { LemonInputSelect, LemonInputSelectOption } from 'lib/lemon-ui/LemonInputSelect'
import { teamLogic } from 'scenes/teamLogic'

import { GuestInviteGrant, inviteLogic } from './inviteLogic'

type ResourceType = 'dashboard' | 'insight' | 'notebook'

function useResourceOptions(resourceType: ResourceType): { options: LemonInputSelectOption[]; loading: boolean } {
    const [options, setOptions] = useState<LemonInputSelectOption[]>([])
    const [loading, setLoading] = useState(false)

    useEffect(() => {
        let cancelled = false
        setLoading(true)
        setOptions([])

        const fetchResources = async (): Promise<void> => {
            try {
                if (resourceType === 'dashboard') {
                    const response = await api.get('api/projects/@current/dashboards/?limit=100')
                    if (!cancelled) {
                        setOptions(
                            (response.results ?? []).map((d: any) => ({
                                key: String(d.id),
                                label: d.name || `Dashboard ${d.id}`,
                            }))
                        )
                    }
                } else if (resourceType === 'insight') {
                    const response = await api.get('api/projects/@current/insights/?limit=100')
                    if (!cancelled) {
                        setOptions(
                            (response.results ?? []).map((i: any) => ({
                                // Insights address by short_id in URLs; that's what the grant stores.
                                key: String(i.short_id || i.id),
                                label: i.name || i.derived_name || `Insight ${i.short_id || i.id}`,
                            }))
                        )
                    }
                } else if (resourceType === 'notebook') {
                    const response = await api.get('api/projects/@current/notebooks/?limit=100')
                    if (!cancelled) {
                        setOptions(
                            (response.results ?? []).map((n: any) => ({
                                key: String(n.short_id),
                                label: n.title || `Notebook ${n.short_id}`,
                            }))
                        )
                    }
                }
            } catch {
                // Leave options empty on error; invites will fail validation downstream.
            } finally {
                if (!cancelled) {
                    setLoading(false)
                }
            }
        }

        void fetchResources()
        return () => {
            cancelled = true
        }
    }, [resourceType])

    return { options, loading }
}

export function GuestResourcePicker(): JSX.Element {
    const { guestGrants } = useValues(inviteLogic)
    const { addGuestGrant, removeGuestGrant } = useActions(inviteLogic)
    const { currentTeamId } = useValues(teamLogic)

    const [resourceType, setResourceType] = useState<ResourceType>('dashboard')
    const [selectedKey, setSelectedKey] = useState<string[]>([])

    const { options, loading } = useResourceOptions(resourceType)

    const handleAdd = (): void => {
        if (!selectedKey[0] || !currentTeamId) {
            return
        }
        const key = selectedKey[0]
        const option = options.find((o) => o.key === key)
        if (!option) {
            return
        }
        addGuestGrant({
            team_id: currentTeamId,
            resource: resourceType,
            resource_id: key,
            label: typeof option.label === 'string' ? option.label : key,
        })
        setSelectedKey([])
    }

    const resourceOptions: { value: ResourceType; label: string }[] = [
        { value: 'dashboard', label: 'Dashboard' },
        { value: 'insight', label: 'Insight' },
        { value: 'notebook', label: 'Notebook' },
    ]

    return (
        <div className="space-y-3" data-attr="guest-resource-picker">
            <div className="text-sm font-medium">Grant access to specific resources</div>
            <div className="flex gap-2 items-center">
                <LemonSelect
                    options={resourceOptions}
                    value={resourceType}
                    onChange={(v) => {
                        if (v) {
                            setResourceType(v)
                            setSelectedKey([])
                        }
                    }}
                    className="shrink-0"
                />
                <div className="flex-1">
                    <LemonInputSelect
                        mode="single"
                        placeholder={loading ? 'Loading…' : `Search ${resourceType}s…`}
                        loading={loading}
                        options={options}
                        value={selectedKey}
                        onChange={setSelectedKey}
                    />
                </div>
                <LemonButton
                    type="primary"
                    size="small"
                    onClick={handleAdd}
                    disabledReason={!selectedKey[0] ? 'Select a resource first' : undefined}
                    data-attr="guest-resource-add"
                >
                    Add
                </LemonButton>
            </div>

            {guestGrants.length > 0 && (
                <div className="space-y-1" data-attr="guest-grants-list">
                    {guestGrants.map((grant: GuestInviteGrant, i: number) => (
                        <div key={i} className="flex items-center gap-2 p-2 bg-bg-light rounded border">
                            <span className="text-xs text-muted capitalize">{grant.resource}</span>
                            <span className="flex-1 text-sm">{grant.label || grant.resource_id}</span>
                            <LemonButton
                                size="small"
                                icon={<IconTrash />}
                                status="danger"
                                onClick={() => removeGuestGrant(i)}
                            />
                        </div>
                    ))}
                </div>
            )}
        </div>
    )
}
