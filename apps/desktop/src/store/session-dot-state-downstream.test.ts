import { atom, type WritableAtom } from 'nanostores'
import { afterEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import type * as ComposerStatus from './composer-status'
import { $backgroundRunningSessionIds } from './composer-status'
import { $sessionDotStateById, showsRunningArc } from './session-dot-state'
import { $stalledSessionIds, clearAllSessionStates, publishSessionState } from './session-states'

vi.mock('./composer-status', async importOriginal => ({
  ...(await importOriginal<typeof ComposerStatus>()),
  $backgroundRunningSessionIds: atom<string[]>([])
}))

const background = $backgroundRunningSessionIds as WritableAtom<string[]>

afterEach(() => {
  clearAllSessionStates()
  background.set([])
})

it('keeps working and stalled turns above a background process, then yields when idle', () => {
  const id = 'downstream-dot-priority'
  background.set([id])
  publishSessionState(id, { ...createClientSessionState(id), busy: true })
  expect($sessionDotStateById.get()[id]).toBe('working')
  expect(showsRunningArc($sessionDotStateById.get()[id])).toBe(true)

  $stalledSessionIds.set([id])
  expect($sessionDotStateById.get()[id]).toBe('stalled')
  expect(showsRunningArc($sessionDotStateById.get()[id])).toBe(true)

  publishSessionState(id, { ...createClientSessionState(id), busy: true, needsInput: true })
  expect($sessionDotStateById.get()[id]).toBe('needs-input')
  expect(showsRunningArc($sessionDotStateById.get()[id])).toBe(false)

  $stalledSessionIds.set([])
  publishSessionState(id, { ...createClientSessionState(id), busy: false })
  expect($sessionDotStateById.get()[id]).toBe('background')
})
