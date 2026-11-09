// Keep official Cloudscape fonts on the same origin without allowing data: fonts in CSP.
import { readFileSync, writeFileSync, mkdirSync, copyFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const original = readFileSync(new URL('../node_modules/@cloudscape-design/global-styles/index.css', import.meta.url), 'utf8');
const blocks = original.match(/@font-face\s*\{[^}]+\}/g) ?? [];
if (blocks.length !== 8) throw new Error('Cloudscape font source changed; inspect before rebuilding.');
const directory = join(root, 'public', 'fonts');
mkdirSync(directory, { recursive: true });
const output = blocks.map((block, index) => {
  const match = block.match(/data:font\/woff2;base64,([A-Za-z0-9+/=]+)/);
  if (!match) throw new Error('Expected the pinned official woff2 font.');
  const file = `open-sans-cloudscape-${index + 1}.woff2`;
  writeFileSync(join(directory, file), Buffer.from(match[1], 'base64'));
  return block.replace(/url\([^)]*\)/, `url('/fonts/${file}')`).replace(/\n}/, '\n  font-display: swap;\n}');
}).join('\n\n');
writeFileSync(join(root, 'src', 'font-faces.css'), `/* Generated from pinned official @cloudscape-design/global-styles. */\n${output}\n`);
copyFileSync(new URL('../node_modules/@cloudscape-design/global-styles/THIRD-PARTY-LICENSES.txt', import.meta.url), join(directory, 'THIRD-PARTY-LICENSES.txt'));
console.log('Prepared 8 self-hosted official Cloudscape Open Sans font assets.');
