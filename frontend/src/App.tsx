import { useEffect, useState } from 'react'

import { api } from './api/client'
import { NewProjectDialog, type NewProjectPayload } from './components/NewProjectDialog'
import { ProjectDashboard } from './pages/ProjectDashboard'
import type { ProjectSummary } from './types'

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [dialogOpen, setDialogOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    try {
      setProjects(await api.listProjects())
      setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  useEffect(() => { void refresh() }, [])

  async function createProject(payload: NewProjectPayload) {
    const project = await api.createProject(payload)
    if (payload.mode === 'run') await api.startProject(project.id)
    setDialogOpen(false)
    await refresh()
  }

  function openProject(projectId: string) {
    window.history.pushState({}, '', `/projects/${projectId}`)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }

  return (
    <>
      {error && <div className="error-banner">{error}</div>}
      <ProjectDashboard projects={projects} onOpen={openProject} onCreate={() => setDialogOpen(true)} />
      <NewProjectDialog
        open={dialogOpen}
        providers={['codex', 'claude', 'kimi']}
        onSubmit={createProject}
        onClose={() => setDialogOpen(false)}
      />
    </>
  )
}
