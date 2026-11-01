// 截图脚本：登录后台 / 学生端，把关键页面各拍一张
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:5055';
const OUT = process.argv[2] || 'shots';
const EXE = process.env.CHROME_PATH;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const shot = async (name, opts = {}) => {
    await page.waitForTimeout(350);
    await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: !!opts.full });
    console.log('  shot', name);
  };

  // --- 后台 ---
  await page.goto(BASE + '/admin/login', { waitUntil: 'networkidle' });
  await shot('admin-login');
  await page.fill('input[name=username]', 'admin');
  await page.fill('input[name=password]', 'admin123');
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('button[type=submit]')]);
  await shot('admin-dashboard', { full: true });

  const links = [
    ['/admin/students', 'admin-students'],
    ['/admin/exams/7/questions', 'admin-questions'],
    ['/admin/exams/7/results', 'admin-results'],
    ['/admin/exams/7/monitor', 'admin-monitor'],
    ['/admin/exams/7/stats', 'admin-stats'],
    ['/admin/exams/7/edit', 'admin-exam-edit'],
    ['/admin/admins', 'admin-accounts'],
    ['/admin/audit', 'admin-audit'],
    ['/admin/data', 'admin-data'],
  ];
  for (const [url, name] of links) {
    const resp = await page.goto(BASE + url, { waitUntil: 'networkidle' });
    if (resp && resp.status() < 400) await shot(name, { full: true });
    else console.log('  skip', name, resp && resp.status());
  }

  // --- 学生端 ---
  const sctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const sp = await sctx.newPage();
  await sp.goto(BASE + '/login', { waitUntil: 'networkidle' });
  await sp.screenshot({ path: path.join(OUT, 'student-login.png') });
  await sp.fill('input[name=username]', '宋铭浩');
  await sp.fill('input[name=password]', '123456');
  await Promise.all([sp.waitForNavigation({ waitUntil: 'networkidle' }), sp.click('button[type=submit]')]);
  await sp.screenshot({ path: path.join(OUT, 'student-dashboard.png'), fullPage: true });
  console.log('  shot student-dashboard');

  for (const [url, name] of [['/profile', 'student-profile'], ['/student/growth', 'student-growth'],
                             ['/student/wrong-book', 'student-wrongbook']]) {
    const r = await sp.goto(BASE + url, { waitUntil: 'networkidle' });
    if (r && r.status() < 400) { await sp.screenshot({ path: path.join(OUT, name + '.png'), fullPage: true }); console.log('  shot', name); }
    else console.log('  skip', name, r && r.status());
  }

  // 手机端
  const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const mp = await mctx.newPage();
  await mp.goto(BASE + '/login', { waitUntil: 'networkidle' });
  await mp.fill('input[name=username]', '宋铭浩');
  await mp.fill('input[name=password]', '123456');
  await Promise.all([mp.waitForNavigation({ waitUntil: 'networkidle' }), mp.click('button[type=submit]')]);
  await mp.screenshot({ path: path.join(OUT, 'mobile-dashboard.png'), fullPage: true });
  console.log('  shot mobile-dashboard');

  await browser.close();
})();
