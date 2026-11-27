// 把 docs/architecture.html 輸出成 docs/architecture.png（4800×2700，海報可用）
// 用法（專案根目錄）：npm i playwright && npx playwright install chromium && node docs/render_diagram.js
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 2400, height: 1350 }, deviceScaleFactor: 2 });
  await page.goto('file://' + path.resolve(__dirname, 'architecture.html'));
  await page.screenshot({ path: path.resolve(__dirname, 'architecture.png') });
  await browser.close();
})();
