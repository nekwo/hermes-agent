import { describe, expect, it } from 'vitest'

import { sessionDotState, sessionShowsRunningArc } from './session-row-state'

describe('session row running appearance', () => {
  it('keeps the running arc when an authoritative turn becomes quiet', () => {
    expect(sessionShowsRunningArc({ isWorking: true, needsInput: false })).toBe(true)
    expect(
      sessionDotState({
        hasBackground: false,
        isStalled: true,
        isUnread: false,
        isWorking: true,
        needsInput: false
      })
    ).toBe('stalled')
  })

  it('uses the needs-input treatment instead of the running arc', () => {
    expect(sessionShowsRunningArc({ isWorking: true, needsInput: true })).toBe(false)
    expect(
      sessionDotState({
        hasBackground: true,
        isStalled: true,
        isUnread: true,
        isWorking: true,
        needsInput: true
      })
    ).toBe('needs-input')
  })

  it('keeps background and unread states below active-turn states', () => {
    expect(
      sessionDotState({
        hasBackground: true,
        isStalled: false,
        isUnread: true,
        isWorking: false,
        needsInput: false
      })
    ).toBe('background')
  })

  // The case above names the ordering but never exercises it: with isWorking
  // false there is no active-turn state for background to lose to. Two desktop
  // E2E specs read that gap as permission to poll the "Background task
  // running" dot as a turn-is-running signal, and then waited 30s for a label
  // the running turn makes unreachable. State the ordering with a turn that is
  // actually running.
  it('shows a working turn holding a live background process as working', () => {
    expect(
      sessionDotState({
        hasBackground: true,
        isStalled: false,
        isUnread: false,
        isWorking: true,
        needsInput: false
      })
    ).toBe('working')

    // A quiet-but-authoritative turn keeps the dot too — a live background
    // process does not take it from a stalled turn either.
    expect(
      sessionDotState({
        hasBackground: true,
        isStalled: true,
        isUnread: false,
        isWorking: true,
        needsInput: false
      })
    ).toBe('stalled')

    // The background dot is reachable only once the turn goes idle — the
    // transition the cross-session E2E spec actually watches.
    expect(
      sessionDotState({
        hasBackground: true,
        isStalled: false,
        isUnread: false,
        isWorking: false,
        needsInput: false
      })
    ).toBe('background')
  })
})
