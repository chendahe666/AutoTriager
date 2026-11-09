import type { ConversationAnswer } from './conversationTypes';
import type { MemoryCitation, MemoryRetrievalItem } from './knowledgeTypes';

export function historicalMemorySources(answer: ConversationAnswer | null) {
  const snapshot = answer?.memory_retrieval;
  const cited: { citation: MemoryCitation; item: MemoryRetrievalItem; number: number }[] = [];
  const rejected: MemoryCitation[] = [];
  const seen = new Set<string>();
  for (const citation of answer?.memory_citations ?? []) {
    const key = JSON.stringify([citation.memory_id, citation.revision]);
    if (seen.has(key)) continue;
    seen.add(key);
    const item = snapshot?.items.find(value => value.memory_id === citation.memory_id && value.revision === citation.revision && value.source.incident_id === citation.source_incident_id && value.source.run_id === citation.source_run_id);
    if (!item) rejected.push(citation);
    else cited.push({ citation, item, number: cited.length + 1 });
  }
  return { snapshot, cited, rejected };
}
