import { useActions, useValues } from 'kea'
import { router } from 'kea-router'
import { useEffect } from 'react'

import { IconDashboard, IconGraph, IconNotebook } from '@posthog/icons'

import { LemonButton } from 'lib/lemon-ui/LemonButton'
import { SceneExport } from 'scenes/sceneTypes'

import { GuestGrant } from '~/types'

import { guestSceneLogic } from './guestSceneLogic'

function grantUrl(grant: GuestGrant): string {
    const { team_id, resource, resource_id } = grant
    if (resource === 'dashboard') {
        return `/project/${team_id}/dashboard/${resource_id}`
    }
    if (resource === 'insight') {
        return `/project/${team_id}/insights/${resource_id}`
    }
    if (resource === 'notebook') {
        return `/project/${team_id}/notebooks/${resource_id}`
    }
    return `/project/${team_id}`
}

function grantIcon(resource: GuestGrant['resource']): JSX.Element {
    if (resource === 'dashboard') {
        return <IconDashboard fontSize="20" />
    }
    if (resource === 'insight') {
        return <IconGraph fontSize="20" />
    }
    return <IconNotebook fontSize="20" />
}

function GrantCard({ grant }: { grant: GuestGrant }): JSX.Element {
    const label = grant.resource_name || `${grant.resource} ${grant.resource_id}`
    return (
        <LemonButton
            type="secondary"
            to={grantUrl(grant)}
            icon={grantIcon(grant.resource)}
            className="w-full justify-start"
            size="medium"
        >
            <span className="flex flex-col items-start text-left">
                <span className="font-medium capitalize">{label}</span>
                <span className="text-xs text-muted capitalize">{grant.resource}</span>
            </span>
        </LemonButton>
    )
}

export function GuestLandingScene(): JSX.Element {
    const { grants, hasMultipleGrants, grantsByProject } = useValues(guestSceneLogic)
    const { push } = useActions(router)

    useEffect(() => {
        if (grants.length === 1) {
            push(grantUrl(grants[0]))
        }
    }, [grants, push])

    if (grants.length === 0) {
        return (
            <div className="flex flex-col items-center justify-center gap-4 p-8 mx-auto max-w-2xl">
                <h1 className="text-2xl font-bold">Your shared content</h1>
                <p className="text-muted">No shared content is available to you at this time.</p>
            </div>
        )
    }

    if (!hasMultipleGrants) {
        // Single grant — redirect in progress via useEffect; avoid a flash of the list.
        return <></>
    }

    return (
        <div className="flex flex-col gap-6 p-8 max-w-2xl mx-auto">
            <div>
                <h1 className="text-2xl font-bold">Your shared content</h1>
                <p className="text-muted">Pick one of the resources below to view.</p>
            </div>
            {Object.entries(grantsByProject).map(([teamId, projectGrants]) => {
                const projectName = projectGrants[0]?.team_name || `Project ${teamId}`
                return (
                    <div key={teamId} className="flex flex-col gap-2">
                        <h2 className="text-sm font-semibold text-muted uppercase tracking-wide">{projectName}</h2>
                        <div className="flex flex-col gap-2">
                            {projectGrants.map((grant, i) => (
                                <GrantCard key={`${grant.resource}:${grant.resource_id}:${i}`} grant={grant} />
                            ))}
                        </div>
                    </div>
                )
            })}
        </div>
    )
}

export const scene: SceneExport = {
    component: GuestLandingScene,
    logic: guestSceneLogic,
}
