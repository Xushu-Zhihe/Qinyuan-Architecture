import type { ProjectEvent } from '../types'

export type ProjectSocketStatus = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface ProjectSocket {
  close: () => void
}

export function connectProjectSocket(
  projectId: string,
  after: number,
  onEvent: (event: ProjectEvent) => void,
  onStatus: (status: ProjectSocketStatus) => void = () => {},
): ProjectSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  let lastSequence = after
  let stopped = false
  let attempts = 0
  let socket: WebSocket | null = null
  let reconnectTimer = 0

  function open() {
    if (stopped) return
    onStatus(attempts === 0 ? 'connecting' : 'reconnecting')
    socket = new WebSocket(
      `${protocol}//${window.location.host}/ws/projects/${projectId}?after=${lastSequence}`,
    )
    socket.addEventListener('open', () => {
      attempts = 0
      onStatus('open')
    })
    socket.addEventListener('message', message => {
      const event = JSON.parse(String(message.data)) as ProjectEvent
      if (event.sequence <= lastSequence) return
      if (event.sequence !== lastSequence + 1) {
        socket?.close()
        return
      }
      lastSequence = event.sequence
      onEvent(event)
    })
    socket.addEventListener('close', () => {
      if (stopped) return
      attempts += 1
      onStatus('reconnecting')
      const delay = Math.min(10_000, 250 * 2 ** Math.min(attempts, 6))
      reconnectTimer = window.setTimeout(open, delay)
    })
    socket.addEventListener('error', () => socket?.close())
  }

  open()
  return {
    close() {
      stopped = true
      window.clearTimeout(reconnectTimer)
      socket?.close()
      onStatus('closed')
    },
  }
}
