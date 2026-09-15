import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { UninstallSection } from './uninstall-section'

// Row 87 (mission-control-queue.md): the uninstall confirm step warns only
// about "$HERMES_HOME" data, never that removing the agent also deletes the
// code checkout's git history, which this tool does not back up.

afterEach(() => {
  cleanup()
  // @ts-expect-error test-only global
  delete window.hermesDesktop
})

function stubBridge(summary: { agent_installed: boolean }) {
  const run = vi.fn().mockResolvedValue({ ok: true })

  // @ts-expect-error test-only global
  window.hermesDesktop = {
    uninstall: {
      summary: vi.fn().mockResolvedValue(summary),
      run
    }
  }

  return { run }
}

describe('UninstallSection', () => {
  it('warns about lost git history when confirming an agent-removing mode', async () => {
    stubBridge({ agent_installed: true })

    render(<UninstallSection />)

    const liteButton = await screen.findByText('Uninstall GUI + agent, keep my data')
    fireEvent.click(liteButton)

    expect(await screen.findByText('Confirm uninstall')).toBeTruthy()
    expect(screen.getByText(/git history/i)).toBeTruthy()
  })

  it('does not warn about git history for the GUI-only mode', async () => {
    stubBridge({ agent_installed: true })

    render(<UninstallSection />)

    const guiButton = await screen.findByText('Uninstall Chat GUI only')
    fireEvent.click(guiButton)

    expect(await screen.findByText('Confirm uninstall')).toBeTruthy()
    expect(screen.queryByText(/git history/i)).toBeNull()
  })
})
