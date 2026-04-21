import { expectLogic } from 'kea-test-utils'

import { initKeaTests } from '~/test/init'

import { userLogic } from '../../userLogic'
import { guestSceneLogic } from '../guestSceneLogic'

describe('guestSceneLogic', () => {
    beforeEach(() => {
        initKeaTests()
        userLogic.mount()
        guestSceneLogic.mount()
    })

    function setUser(user: any): void {
        userLogic.actions.loadUserSuccess(user)
    }

    it('isGuest reflects user.is_guest_in_current_project', async () => {
        setUser({ uuid: 'u1', email: 'a@b.co', is_guest_in_current_project: false, guest_grants: [] })
        await expectLogic(guestSceneLogic).toMatchValues({ isGuest: false })

        setUser({ uuid: 'u1', email: 'a@b.co', is_guest_in_current_project: true, guest_grants: [] })
        await expectLogic(guestSceneLogic).toMatchValues({ isGuest: true })
    })

    it('groups grants by team_id', async () => {
        setUser({
            uuid: 'u1',
            email: 'a@b.co',
            is_guest_in_current_project: true,
            guest_grants: [
                { team_id: 1, resource: 'dashboard', resource_id: '10' },
                { team_id: 1, resource: 'insight', resource_id: 'abc' },
                { team_id: 2, resource: 'notebook', resource_id: 'xyz' },
            ],
        })

        await expectLogic(guestSceneLogic).toMatchValues({
            hasMultipleGrants: true,
            grantsByProject: {
                1: [
                    { team_id: 1, resource: 'dashboard', resource_id: '10' },
                    { team_id: 1, resource: 'insight', resource_id: 'abc' },
                ],
                2: [{ team_id: 2, resource: 'notebook', resource_id: 'xyz' }],
            },
        })
    })

    it('hasMultipleGrants is false for exactly one grant', async () => {
        setUser({
            uuid: 'u1',
            email: 'a@b.co',
            is_guest_in_current_project: true,
            guest_grants: [{ team_id: 1, resource: 'dashboard', resource_id: '10' }],
        })
        await expectLogic(guestSceneLogic).toMatchValues({ hasMultipleGrants: false })
    })

    it('defaults sensibly for a non-guest user', async () => {
        setUser({ uuid: 'u1', email: 'a@b.co' })
        await expectLogic(guestSceneLogic).toMatchValues({
            isGuest: false,
            grants: [],
            grantsByProject: {},
            hasMultipleGrants: false,
        })
    })
})
