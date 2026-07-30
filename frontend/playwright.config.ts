import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import fs from 'node:fs'
import path from 'node:path'

const frontendRoot = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(frontendRoot, '..')
const configuredPython = process.env.QINYUAN_PYTHON
const localPython = process.platform === 'win32'
  ? path.resolve(projectRoot, '.venv', 'Scripts', 'python.exe')
  : path.resolve(projectRoot, '.venv', 'bin', 'python')
const pythonExecutable = configuredPython
  ?? (fs.existsSync(localPython) ? `"${localPython}"` : 'python')
const apiPort = Number(process.env.BLACKBOARD_E2E_API_PORT ?? 8000)
const webPort = Number(process.env.BLACKBOARD_E2E_WEB_PORT ?? 5173)
const dataRoot = path.resolve(
  frontendRoot,
  'test-results',
  `web-data-${process.pid}-${Date.now()}`,
)

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: `${pythonExecutable} -m uvicorn tests.fixtures.web_test_app:create_app --factory --host 127.0.0.1 --port ${apiPort}`,
      cwd: projectRoot,
      env: { ...process.env, BLACKBOARD_E2E_DATA_ROOT: dataRoot },
      url: `http://127.0.0.1:${apiPort}/api/projects`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort}`,
      cwd: frontendRoot,
      url: `http://127.0.0.1:${webPort}`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'narrow', use: { viewport: { width: 390, height: 844 } } },
  ],
})
