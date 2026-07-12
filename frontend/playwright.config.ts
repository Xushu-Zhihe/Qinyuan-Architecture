import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const frontendRoot = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(frontendRoot, '..')
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
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: 'python -m uvicorn tests.fixtures.web_test_app:create_app --factory --host 127.0.0.1 --port 8000',
      cwd: projectRoot,
      env: { ...process.env, BLACKBOARD_E2E_DATA_ROOT: dataRoot },
      url: 'http://127.0.0.1:8000/api/projects',
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      cwd: frontendRoot,
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'narrow', use: { viewport: { width: 390, height: 844 } } },
  ],
})
