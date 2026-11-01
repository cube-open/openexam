// 验证本次 UI 改动：学生端统一顶栏、后台侧栏收起（含刷新记忆）、操作列改详情+删除
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:5055';
const OUT = process.argv[2] || 'shots_ui';
const EXE = process.env.CHROME_PATH;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const shot = async (name, full = false) => {
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: full });
    console.log('  shot', name);
  };

  // --- 后台登录 ---
  await page.goto(BASE + '/admin/login', { waitUntil: 'networkidle' });
  await page.fill('input[name=username]', 'admin');
  await page.fill('input[name=password]', 'admin123');
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('button[type=submit]')]);

  // 1. 默认展开 + 详情/删除 操作列
  await shot('admin-default', true);

  // 2. 点“收起侧栏”
  await page.click('.sidebar-collapse');
  await page.waitForTimeout(400);
  await shot('admin-collapsed', true);
  const hidden = await page.evaluate(() => document.body.classList.contains('sidebar-hidden'));
  console.log('  sidebar-hidden class:', hidden);

  // 3. 刷新后应保持收起（localStorage 记忆）
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(400);
  const stillHidden = await page.evaluate(() => document.body.classList.contains('sidebar-hidden'));
  console.log('  after reload still hidden:', stillHidden);
  await shot('admin-collapsed-reload', true);

  // 4. 点“展开侧栏”恢复
  await page.click('.sidebar-expand');
  await page.waitForTimeout(400);
  const restored = await page.evaluate(() => !document.body.classList.contains('sidebar-hidden'));
  console.log('  expand restored:', restored);
  await shot('admin-expanded-again', true);

  // 5. 试卷详情页（原“编辑”入口）可达
  await page.goto(BASE + '/admin/exams/7/edit', { waitUntil: 'networkidle' });
  await shot('admin-exam-detail', true);

  // 6. 学生端新顶栏（当前页高亮）
  const sctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const sp = await sctx.newPage();
  await sp.goto(BASE + '/login', { waitUntil: 'networkidle' });
  await sp.fill('input[name=username]', '宋铭浩');
  await sp.fill('input[name=password]', '123456');
  await Promise.all([sp.waitForNavigation({ waitUntil: 'networkidle' }), sp.click('button[type=submit]')]);
  await sp.waitForTimeout(300);
  await sp.screenshot({ path: path.join(OUT, 'student-topbar-dashboard.png') });
  console.log('  shot student-topbar-dashboard');
  const activeLabel = await sp.evaluate(() => {
    const a = document.querySelector('.topbar-user a.nav-link.active');
    return a ? a.textContent.trim() : '(none)';
  });
  console.log('  dashboard active nav:', activeLabel);

  await sp.goto(BASE + '/student/wrong-book', { waitUntil: 'networkidle' });
  await sp.waitForTimeout(300);
  await sp.screenshot({ path: path.join(OUT, 'student-topbar-wrongbook.png') });
  const activeLabel2 = await sp.evaluate(() => {
    const a = document.querySelector('.topbar-user a.nav-link.active');
    return a ? a.textContent.trim() : '(none)';
  });
  console.log('  wrongbook active nav:', activeLabel2);

  await browser.close();
  console.log('DONE');
})().catch(e => { console.error(e); process.exit(1); });
