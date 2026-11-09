import { scopedCitations } from './conversation.ts';
import type { ConversationCitation, ConversationTurn } from './conversationTypes';

export type NumberedCitation = ConversationCitation & { number: number };
export type CitationFragment = { kind: 'text'; text: string } | ({ kind: 'citation' } & NumberedCitation);

/** Match accepted IDs only; never convert generated markup or unknown text into links. */
export function conversationCitations(turn: ConversationTurn): {
  sources: NumberedCitation[]; fragments: CitationFragment[]; unknownInlineIds: string[];
} {
  const text = turn.answer?.text ?? '';
  const sources = scopedCitations(turn).accepted.map((reference, index) => ({ ...reference, number: index + 1 }));
  const byId = new Map(sources.map(source => [source.evidence_id, source]));
  // One tokenizer owns both linking and unknown-ID detection. A known prefix in
  // ev_one.other is never a source; a sentence-ending period is punctuation.
  const matches = [...text.matchAll(/[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?/g)];
  const unknownInlineIds = [...new Set(matches.map(match => match[0]).filter(value => /^(?:ev|ep)_/.test(value) && !byId.has(value)))];
  if (sources.length === 0) return { sources, fragments: [{ kind: 'text', text }], unknownInlineIds };
  const fragments: CitationFragment[] = [];
  let cursor = 0;
  for (const match of matches) {
    const source = byId.get(match[0]);
    if (!source) continue;
    let start = match.index!;
    let end = start + match[0].length;
    // Remove adjacent citation presentation wrappers, never parse them as HTML.
    if (start > cursor && text[start - 1] === '`' && text[end] === '`') { start -= 1; end += 1; }
    if (start > cursor && text[start - 1] === '[' && text[end] === ']') { start -= 1; end += 1; }
    if (start > cursor) fragments.push({ kind: 'text', text: text.slice(cursor, start) });
    fragments.push({ kind: 'citation', ...source });
    cursor = end;
  }
  if (cursor < text.length) fragments.push({ kind: 'text', text: text.slice(cursor) });
  return { sources, fragments, unknownInlineIds };
}
