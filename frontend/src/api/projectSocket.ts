import type { ProjectEvent } from '../types'

export function connectProjectSocket(
  projectId: string,
  after: number,
  onEvent: (event: ProjectEvent) => void,
): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(
    `${protocol}//${window.location.host}/ws/projects/${projectId}?after=${after}`,
  )
  socket.addEventListener('message', event => {
    onEvent(JSON.parse(String(event.data)) as ProjectEvent)
  })
  return socket
}
