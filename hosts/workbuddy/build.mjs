// WorkBuddy 送审包构建器（零依赖，node 18+）
// 用法：node hosts/workbuddy/build.mjs
// 产物：dist/wb-skill/（技能包）与 dist/wb-connector/（连接器包）
// 纪律：只做拷贝与组包，不改源文件；连跑两次产物逐字节一致（幂等）。
// 注意：连接器内核为 Python 3.10+ 标准库实现，宿主机需预装 Python（mcp.json 的 command 为 python）。
import { cpSync, rmSync, mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..', '..');
const DIST = path.join(ROOT, 'dist');

function countFiles(dir) {
  let n = 0;
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) n += countFiles(path.join(dir, e.name));
    else n += 1;
  }
  return n;
}

// ---------- 技能包 dist/wb-skill ----------
const SKILL = path.join(DIST, 'wb-skill');
rmSync(SKILL, { recursive: true, force: true });
mkdirSync(path.join(SKILL, 'references'), { recursive: true });
mkdirSync(path.join(SKILL, 'templates'), { recursive: true });
cpSync(path.join(HERE, 'wb-skill', 'SKILL.md'), path.join(SKILL, 'SKILL.md'));
cpSync(path.join(ROOT, 'README.md'), path.join(SKILL, 'references', 'README.md'));
cpSync(path.join(ROOT, 'packs', 'meeting', 'rules.example.yaml'),
        path.join(SKILL, 'templates', 'rules.example.yaml'));
cpSync(path.join(ROOT, 'LICENSE'), path.join(SKILL, 'LICENSE'));

// ---------- 连接器包 dist/wb-connector ----------
const CONN = path.join(DIST, 'wb-connector');
rmSync(CONN, { recursive: true, force: true });
mkdirSync(CONN, { recursive: true });
cpSync(path.join(HERE, 'wb-connector', 'connector-meta.json'), path.join(CONN, 'connector-meta.json'));
cpSync(path.join(HERE, 'wb-connector', 'mcp.json'), path.join(CONN, 'mcp.json'));
cpSync(path.join(HERE, 'icon.svg'), path.join(CONN, 'icon.svg'));
mkdirSync(path.join(CONN, 'hosts', 'mcp'), { recursive: true });
cpSync(path.join(ROOT, 'hosts', 'mcp', 'meeting_server.py'),
        path.join(CONN, 'hosts', 'mcp', 'meeting_server.py'));
mkdirSync(path.join(CONN, 'core', 'py'), { recursive: true });
for (const f of ['quickcue_engine.py', 'feedback.py']) {
  cpSync(path.join(ROOT, 'core', 'py', f), path.join(CONN, 'core', 'py', f));
}
mkdirSync(path.join(CONN, 'skills'), { recursive: true });
cpSync(path.join(HERE, 'wb-skill', 'SKILL.md'), path.join(CONN, 'skills', 'SKILL.md'));
cpSync(path.join(ROOT, 'LICENSE'), path.join(CONN, 'LICENSE'));

console.log(`[build] dist/wb-skill: ${countFiles(SKILL)} 件`);
console.log(`[build] dist/wb-connector: ${countFiles(CONN)} 件`);
console.log('[build] 完成——上传时把两个目录分别打成 zip');
