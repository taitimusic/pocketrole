import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

function extractFunctionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} not found`);

  const bodyStart = source.indexOf('{', start);
  assert.notEqual(bodyStart, -1, `${name} body not found`);

  let depth = 0;
  for (let i = bodyStart; i < source.length; i += 1) {
    const char = source[i];
    if (char === '{') depth += 1;
    if (char === '}') depth -= 1;
    if (depth === 0) {
      return source.slice(start, i + 1);
    }
  }
  throw new Error(`${name} body did not terminate`);
}

function loadFunction(path, name) {
  const source = readFileSync(resolve(ROOT, path), 'utf8');
  const functionSource = extractFunctionSource(source, name);
  const context = {};
  vm.runInNewContext(`${functionSource}; globalThis.__fn = ${name};`, context);
  return context.__fn;
}

function loadPublicMessageRenderer() {
  const source = readFileSync(resolve(ROOT, 'web/assets/app.js'), 'utf8');
  const linkifySource = extractFunctionSource(source, 'linkify');
  const currentAffairsSource = extractFunctionSource(source, 'renderCurrentAffairsLink');
  const renderMessageSource = extractFunctionSource(source, 'renderMessageHtml');
  const context = {};
  vm.runInNewContext(
    `${linkifySource}\n${currentAffairsSource}\n${renderMessageSource}; globalThis.__fn = renderMessageHtml;`,
    context
  );
  return context.__fn;
}

const markdownLog =
  '「[カレー屋店主インド人「助けて！30年間日本のルールを守って頑張ってきたのに日本政府が急にビザ取り上げてきた！」→有志が署名約5.3万筆を提出](https://jin115.com/archives/52450744.html)」まるで、全てが計画通りに進まなかった時のような、徒労な抵抗だ。';

const expectedLinkedLog =
  '「<a href="https://jin115.com/archives/52450744.html" target="_blank" rel="noopener noreferrer">カレー屋店主インド人「助けて！30年間日本のルールを守って頑張ってきたのに日本政府が急にビザ取り上げてきた！」→有志が署名約5.3万筆を提出</a>」まるで、全てが計画通りに進まなかった時のような、徒労な抵抗だ。';

for (const [label, path, functionName] of [
  ['public viewer', 'web/assets/app.js', 'linkify'],
  ['admin viewer', 'admin/assets/admin_app.js', 'viewerLinkify'],
]) {
  test(`${label} renders current_affairs markdown links without double-linkifying href`, () => {
    const linkify = loadFunction(path, functionName);
    const rendered = linkify(markdownLog);

    assert.equal(rendered, expectedLinkedLog);
    assert.doesNotMatch(rendered, /href="<a href=/);
  });

  test(`${label} still linkifies bare URLs`, () => {
    const linkify = loadFunction(path, functionName);
    const rendered = linkify('出典 https://example.com/news/123');

    assert.equal(
      rendered,
      '出典 <a href="https://example.com/news/123" target="_blank" rel="noopener noreferrer">https://example.com/news/123</a>'
    );
  });
}

test('public viewer renders current_affairs as a news title link without visible URL text', () => {
  const renderMessageHtml = loadPublicMessageRenderer();
  const rendered = renderMessageHtml(markdownLog);

  assert.match(rendered, /class="news-link-title"/);
  assert.match(rendered, /href="https:\/\/jin115\.com\/archives\/52450744\.html"/);
  assert.match(rendered, />カレー屋店主インド人/);
  assert.match(rendered, /まるで、全てが計画通りに進まなかった時/);
  assert.doesNotMatch(rendered, /\]\(https:\/\/jin115\.com\/archives\/52450744\.html\)/);
  assert.doesNotMatch(rendered, />https:\/\/jin115\.com\/archives\/52450744\.html</);
});
