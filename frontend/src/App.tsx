import { useEffect, useState } from 'react'

import { api } from './api/client'
import { NewProjectDialog, type NewProjectPayload } from './components/NewProjectDialog'
import { ProjectDashboard } from './pages/ProjectDashboard'
import { BlackboardWorkspace } from './pages/BlackboardWorkspace'
import { AutopilotPage } from './pages/AutopilotPage'
import type { ProjectSummary, RuntimeSnapshot } from './types'

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [runtime, setRuntime] = useState<RuntimeSnapshot | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [path, setPath] = useState(window.location.pathname)

  async function refresh() {
    try {
      const [nextProjects, nextRuntime] = await Promise.all([
        api.listProjects(),
        api.getRuntime(),
      ])
      setProjects(nextProjects)
      setRuntime(nextRuntime)
      setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 3000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    const listener = () => setPath(window.location.pathname)
    window.addEventListener('popstate', listener)
    return () => window.removeEventListener('popstate', listener)
  }, [])

  async function createProject(payload: NewProjectPayload) {
    const project = await api.createProject(payload)
    if (payload.mode === 'run') await api.startProject(project.id)
    setDialogOpen(false)
    await refresh()
  }

  async function deleteProject(projectId: string) {
    try {
      setError(null)
      await api.deleteProject(projectId)
      await refresh()
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught)
      setError(message)
      throw caught
    }
  }

  function openProject(projectId: string) {
    window.history.pushState({}, '', `/projects/${projectId}`)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }

  function openAutopilot() {
    window.history.pushState({}, '', '/auto')
    window.dispatchEvent(new PopStateEvent('popstate'))
  }

  function goDashboard() {
    window.history.pushState({}, '', '/')
    window.dispatchEvent(new PopStateEvent('popstate'))
  }

  const projectMatch = path.match(/^\/projects\/([^/]+)$/)
  if (projectMatch) {
    return <BlackboardWorkspace projectId={projectMatch[1]} onBack={goDashboard} />
  }

  if (path === '/auto') {
    return <AutopilotPage onBack={goDashboard} onOpenProject={openProject} />
  }

  return (
    <>
      {error && <div className="error-banner">{error}</div>}
      <ProjectDashboard projects={projects} runtime={runtime} onOpen={openProject} onCreate={() => setDialogOpen(true)} onDelete={deleteProject} onOpenAutopilot={openAutopilot} />
      <NewProjectDialog
        open={dialogOpen}
        providers={['codex', 'claude', 'kimi', 'qinyuan-agent']}
        onSubmit={createProject}
        onClose={() => setDialogOpen(false)}
      />
    </>
  )
}
