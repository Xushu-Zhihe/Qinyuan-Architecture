import { expect, test, type Page, type TestInfo, type WebSocketRoute } from '@playwright/test'

async function createProject(
  page: Page,
  name: string,
  mode: 'manual' | 'run',
  provider: string,
) {
  await page.goto('/')
  await page.getByRole('button', { name: '新建项目' }).click()
  await page.getByLabel('项目名称').fill(name)
  await page.getByLabel('Goal').fill('Recover the flag')
  await page.getByLabel('Origin').fill('Deterministic E2E challenge')
  if (mode === 'run') await page.getByLabel('立即启动 Agent').click()
  await page.getByLabel(provider).check()
  await page.getByRole('button', { name: mode === 'run' ? '创建并启动' : '创建项目' }).click()
  const projectCard = page.locator('.project-card__open').filter({ hasText: name })
  await expect(projectCard).toBeVisible()
  await projectCard.click()
  await expect(page.locator('.workspace-title strong')).toHaveText(name)
}

async function expectViewportContained(page: Page) {
  const dimensions = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(dimensions.width).toBeLessThanOrEqual(dimensions.viewport)
}

async function expectCanvasHasVisibleNodes(page: Page) {
  const canvas = page.locator('.graph-canvas')
  const nodes = page.locator('.react-flow__node')
  await expect(canvas).toBeVisible()
  await expect(nodes.first()).toBeVisible()
  const canvasBox = await canvas.boundingBox()
  expect(canvasBox).not.toBeNull()
  for (let index = 0; index < await nodes.count(); index += 1) {
    const nodeBox = await nodes.nth(index).boundingBox()
    expect(nodeBox).not.toBeNull()
    expect(nodeBox!.x).toBeGreaterThanOrEqual(canvasBox!.x)
    expect(nodeBox!.x + nodeBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width)
    expect(nodeBox!.y).toBeGreaterThanOrEqual(canvasBox!.y)
    expect(nodeBox!.y + nodeBox!.height).toBeLessThanOrEqual(canvasBox!.y + canvasBox!.height)
  }
}

async function createRunningProject(page: Page, name: string, provider: string) {
  await createProject(page, name, 'run', provider)
  await expect(page.getByText(`${provider} output`, { exact: true })).toBeVisible()
  await page.locator('.blackboard-node--fact', { hasText: `${provider} output` }).click()
  await expect(page.getByLabel('Agent 实时日志').getByText(`${provider} output`, { exact: false })).toBeVisible()
}

test('project overview stays inside desktop and narrow viewports', async ({ page }, testInfo) => {
  await page.goto('/')
  await expect(page.getByText('BLACKBOARD', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '运行总览' })).toBeVisible()
  await expectViewportContained(page)
  await page.screenshot({ path: testInfo.outputPath(`overview-${testInfo.project.name}.png`), fullPage: true })
})

test('runtime diagnostics expose planning, execution and audit controls', async ({ page }, testInfo) => {
  await createRunningProject(page, `Diagnostics ${testInfo.project.name}`, 'codex')
  await page.getByRole('button', { name: '诊断' }).click()
  await expect(page.getByRole('heading', { name: '运行诊断' })).toBeVisible()
  await expect(page.getByText(/^execution-/).first()).toBeVisible()

  await page.getByRole('tab', { name: /Planning/ }).click()
  await expect(page.getByText(/^planning-/).first()).toBeVisible()
  await page.getByText(/^planning-/).first().click()
  await expect(page.getByLabel('Agent 实时日志').getByText('codex planning', { exact: false })).toBeVisible()

  await page.getByRole('button', { name: /^定位 intent-/ }).first().click()
  await expect(page.getByText(/^Intent intent-/).first()).toBeVisible()
  await expect(page.getByRole('link', { name: '导出审计包' })).toBeVisible()
  await expectViewportContained(page)
  await page.getByRole('button', { name: '诊断' }).click()
  await page.screenshot({ path: testInfo.outputPath(`diagnostics-${testInfo.project.name}.png`), fullPage: true })
})

test('two running projects keep graph and logs isolated', async ({ browser }, testInfo: TestInfo) => {
  const context = await browser.newContext()
  const first = await context.newPage()
  const second = await context.newPage()
  const firstName = `Alpha ${testInfo.project.name}`
  const secondName = `Beta ${testInfo.project.name}`

  await createRunningProject(first, firstName, 'codex')
  await createRunningProject(second, secondName, 'claude')

  await expect(first.getByText(firstName)).toBeVisible()
  await expect(first.getByText('claude output', { exact: true })).not.toBeVisible()
  await expect(second.getByText(secondName)).toBeVisible()
  await expect(second.getByText('codex output', { exact: true })).not.toBeVisible()
  await expectCanvasHasVisibleNodes(first)
  await expectCanvasHasVisibleNodes(second)
  await expectViewportContained(first)
  await expectViewportContained(second)
  await first.screenshot({ path: testInfo.outputPath(`workspace-${testInfo.project.name}.png`), fullPage: true })
  await context.close()
})

test('live logs recover after the browser websocket reconnects', async ({ page }, testInfo) => {
  const provider = 'codex'
  const sockets: WebSocketRoute[] = []
  let releaseReconnect = () => {}
  const reconnectGate = new Promise<void>(resolve => { releaseReconnect = resolve })
  await page.routeWebSocket(/\/ws\/projects\//, async socket => {
    sockets.push(socket)
    if (sockets.length > 1) await reconnectGate
    socket.connectToServer()
  })
  await createRunningProject(page, `Reconnect ${testInfo.project.name}`, provider)
  await expect.poll(() => sockets.length).toBe(1)

  await sockets[0].close({ code: 1012, reason: 'E2E reconnect check' })
  await expect(page.getByLabel('Agent 实时日志').getByText('重连中')).toBeVisible()
  await expect.poll(() => sockets.length).toBe(2)
  releaseReconnect()

  await expect(page.getByLabel('Agent 实时日志').getByText('重连中')).toHaveCount(0, { timeout: 15_000 })
  await expect(page.getByLabel('Agent 实时日志').getByText(`${provider} output`, { exact: false })).toBeVisible()
})

test('manual board can add fact and intent before agents start', async ({ page }, testInfo) => {
  const name = `Manual blackboard ${testInfo.project.name} with a deliberately long project name`
  const fact = 'JWT uses HS256 and this deliberately long fact remains readable inside the graph node'
  await createProject(page, name, 'manual', 'codex')
  await page.getByRole('button', { name: '编辑' }).click()

  page.once('dialog', dialog => dialog.accept(fact))
  await page.getByRole('button', { name: '添加 Fact' }).click()
  await expect(page.getByText(fact, { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '适应画布' }).click()

  await page.getByRole('button', { name: '连接节点' }).click()
  await page.locator('.blackboard-node--fact').click()
  page.once('dialog', dialog => dialog.accept('Find the signing secret'))
  await page.locator('.blackboard-node--goal').click()

  await expect(page.getByText('Find the signing secret', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '启动 Agent' }).click()
  await expect(page.locator('.workspace-title .status')).toHaveText('running')
  await expectCanvasHasVisibleNodes(page)
  await expectViewportContained(page)
  await page.locator('.react-flow__pane').click({ position: { x: 18, y: 18 } })
  if ((page.viewportSize()?.width ?? 0) <= 820) {
    await expect(page.locator('.inspector--empty')).toBeHidden()
  }
  await page.screenshot({ path: testInfo.outputPath(`manual-workspace-${testInfo.project.name}.png`), fullPage: true })
})

test('project can be moved to the recoverable trash from its card', async ({ page }, testInfo) => {
  const name = `Delete ${testInfo.project.name}`
  await createProject(page, name, 'manual', 'codex')
  await page.getByRole('button', { name: '返回项目总览' }).click()

  await page.getByRole('button', { name: `删除项目 ${name}` }).click()
  await expect(page.getByRole('dialog', { name: '删除项目' })).toContainText(name)
  await page.getByRole('button', { name: '移入回收目录' }).click()

  await expect(page.locator('.project-card__open').filter({ hasText: name })).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.project-card__open').filter({ hasText: name })).toHaveCount(0)
})

test('edit mode archives, restores and deletes server-backed graph elements', async ({ page }, testInfo) => {
  const name = `Graph edit ${testInfo.project.name}`
  const fact = `Archivable fact ${testInfo.project.name}`
  const intent = `Disposable intent ${testInfo.project.name}`
  await createProject(page, name, 'manual', 'codex')
  await page.getByRole('button', { name: '编辑' }).click()

  page.once('dialog', dialog => dialog.accept(fact))
  await page.getByRole('button', { name: '添加 Fact' }).click()
  const factNode = page.locator('.blackboard-node--fact', { hasText: fact })
  await expect(factNode).toBeVisible()

  await page.getByRole('button', { name: '连接节点' }).click()
  await factNode.click()
  page.once('dialog', dialog => dialog.accept(intent))
  await page.locator('.blackboard-node--goal').click()
  await page.getByRole('button', { name: `选择 Intent ${intent}` }).click()
  await expect(page.locator('.graph-toolbar').getByRole('button', { name: '删除边' })).toBeEnabled()
  page.once('dialog', dialog => dialog.accept())
  await page.locator('.graph-toolbar').getByRole('button', { name: '删除边' }).click()
  await expect(page.getByText(intent, { exact: true })).toHaveCount(0)

  await factNode.click()
  page.once('dialog', dialog => dialog.accept())
  await page.locator('.graph-toolbar').getByRole('button', { name: '归档节点' }).click()
  await expect(factNode).toHaveCount(0)

  await page.getByLabel('显示已归档').check()
  const archivedFact = page.locator('.blackboard-node.is-archived', { hasText: fact })
  await expect(archivedFact).toBeVisible()
  await archivedFact.click()
  await page.locator('.graph-toolbar').getByRole('button', { name: '恢复节点' }).click()
  await expect(page.locator('.blackboard-node--fact:not(.is-archived)', { hasText: fact })).toBeVisible()
  await page.reload()
  await expect(page.locator('.blackboard-node--fact:not(.is-archived)', { hasText: fact })).toBeVisible()
})
