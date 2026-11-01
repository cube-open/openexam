// 答题页 / 成绩页验收：临时建卷，跑完整的学生答题流程并截图
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:5055';
const OUT = process.argv[2] || 'shots';
const EXE = process.env.CHROME_PATH;

const QUESTIONS = [
  { type: 'choice', question: '下面哪个是时间复杂度的 **渐进上界**？',
    options: ['O(n)', 'O(n log n)', 'O(n^2)', 'O(1)'], answer: 'A', score: 5,
    explanation: '渐进上界描述最坏情况下的增长趋势。' },
  { type: 'multi', question: '下列属于平衡树的有：',
    options: ['AVL 树', '红黑树', 'B+ 树', '堆'], answer: 'AB', score: 5,
    explanation: 'B+ 树是索引结构，堆不是平衡有序树。' },
  { type: 'judge', question: '归并排序在最坏情况下的时间复杂度是 O(n log n)。',
    options: ['正确', '错误'], answer: 'A', score: 5,
    explanation: '归并排序无论最好最坏都是 O(n log n)。' },
  { type: 'blank', question: '操作系统中，进程调度的时间单位是 ______。',
    options: [], answer: '时间片', score: 5,
    explanation: '时间片轮转调度以时间片为单位分配 CPU。' },
  { type: 'group', question: '二、简答题', options: [], answer: '', score: 20,
    children: [
      { type: 'blank', question: '请写出归并排序的伪代码。', options: [],
        answer: 'merge', score: 10, explanation: '先递归再归并。' },
      { type: 'blank', question: '请解释什么是「稳定排序」。', options: [],
        answer: '稳定', score: 10, explanation: '相等元素相对次序不变。' }
    ]
  }
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE });
  const shot = async (page, name, full = true) => {
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: full });
    console.log('  shot', name);
  };
  const login = async (page, user, pwd) => {
    await page.goto(BASE + '/login', { waitUntil: 'networkidle' });
    await page.fill('input[name=username]', user);
    await page.fill('input[name=password]', pwd);
    await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }),
                       page.click('button[type=submit]')]);
  };

  // ---------- 建卷（后台） ----------
  const actx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const ap = await actx.newPage();
  await ap.goto(BASE + '/admin/login', { waitUntil: 'networkidle' });
  await ap.fill('input[name=username]', 'admin');
  await ap.fill('input[name=password]', 'admin123');
  await Promise.all([ap.waitForNavigation({ waitUntil: 'networkidle' }), ap.click('button[type=submit]')]);

  const buildExam = async (label) => {
    const title = label + ' ' + Date.now();
    await ap.goto(BASE + '/admin', { waitUntil: 'networkidle' });
    await ap.fill('input[name=title]', title);
    await ap.fill('input[name=description]', '答题页排版验收用');
    await ap.fill('input[name=duration_minutes]', '30');
    await Promise.all([ap.waitForNavigation({ waitUntil: 'networkidle' }),
                        ap.click('form.inline-form button[type=submit]')]);
    const examId = await ap.evaluate((t) => {
      const rows = Array.from(document.querySelectorAll('table.table tbody tr'));
      const row = rows.find((r) => r.textContent.includes(t));
      if (!row) return null;
      const a = row.querySelector('a[href*="/questions"]');
      return a ? a.getAttribute('href').match(/exams\/(\d+)\/questions/)[1] : null;
    }, title);
    await ap.goto(BASE + '/admin/exams/' + examId + '/questions', { waitUntil: 'networkidle' });
    await ap.fill('textarea[name=bulk]', JSON.stringify(QUESTIONS));
    await Promise.all([
      ap.waitForNavigation({ waitUntil: 'networkidle' }),
      ap.click('form[action$="/questions/import"] button[type=submit]')
    ]);
    const body = await ap.textContent('body');
    console.log('  试卷 id=' + examId, (body.match(/导入完成[：:][^。]*。/) || ['?'])[0]);
    return { examId, title };
  };

  // 从考试大厅按「开始考试」走真实流程（start 路由只收 POST）
  const startExam = async (page, title) => {
    await page.goto(BASE + '/dashboard', { waitUntil: 'networkidle' });
    const card = await page.evaluateHandle((t) => {
      const cards = Array.from(document.querySelectorAll('.exam-card'));
      return cards.find((c) => c.textContent.includes(t)) || null;
    }, title);
    const el = card.asElement();
    if (!el) throw new Error('考试大厅里找不到这张卷：' + title);
    await el.$eval('button', (b) => { if (b.textContent.includes('开始考试') || b.textContent.includes('继续作答')) b.click(); });
    await page.waitForTimeout(400);
    if (await page.$('#start-ok')) {
      await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('#start-ok')]);
    }
    await page.waitForTimeout(600);
  };

  // ---------- 桌面端：完整答题 → 成绩页 ----------
  const a = await buildExam('UI 验收卷');
  const sctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const sp = await sctx.newPage();
  await login(sp, '宋铭浩', '123456');
  await startExam(sp, a.title);
  console.log('  答题页地址:', sp.url());
  await shot(sp, 'exam-page');

  const radios = await sp.$$('.question input[type=radio]');
  if (radios.length) await radios[0].check();
  const checks = await sp.$$('.question input[type=checkbox]');
  if (checks.length >= 2) { await checks[0].check(); await checks[1].check(); }
  for (const b of await sp.$$('.question input.blank-input')) await b.fill('时间片');
  await sp.waitForTimeout(400);
  await shot(sp, 'exam-page-answered');

  await sp.click('#btn-submit');
  await sp.waitForTimeout(400);
  await sp.click('#modal-ok');
  await sp.waitForTimeout(1500);
  console.log('  交卷后地址:', sp.url());
  await shot(sp, 'result-page');

  // ---------- 手机端：另建一张卷，走同样的开场 ----------
  const b = await buildExam('移动端验收卷');
  const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const mp = await mctx.newPage();
  await login(mp, '宋铭浩', '123456');
  await startExam(mp, b.title);
  console.log('  手机答题页地址:', mp.url());
  await shot(mp, 'exam-page-mobile');

  await browser.close();
})();
