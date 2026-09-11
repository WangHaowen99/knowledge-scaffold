import { createOntologyResource } from '../src/app/providers/ontologyResource.js';
import { ontologyRequestPolicy } from '../src/app/providers/ontologyRequestPolicy.js';

let ontologyListRequests = 0;
const resource = createOntologyResource({
  fetcher: async (url) => {
    if (url === '/agentic/ontologies') {
      ontologyListRequests += 1;
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ ontologies: [] }),
    };
  },
});
const scenarios = [
  { surface: 'config', configScope: 'global', configSection: 'llm' },
  { surface: 'chat' },
  { surface: 'kb' },
  { surface: 'config', configScope: 'workspace', configSection: 'mcp' },
  { surface: 'solver' },
];
let nonKnowledgeTreeRequests = 0;
const listLoads = [];
for (const scenario of scenarios) {
  const policy = ontologyRequestPolicy(scenario);
  if (scenario.surface !== 'kb' && policy.treeEnabled) {
    nonKnowledgeTreeRequests += 1;
  }
  if (policy.listEnabled) {
    listLoads.push(
      resource.load({
        principalId: 'synthetic-principal',
        workspaceId: 'synthetic-workspace',
        revision: 1,
      }),
    );
  }
}
await Promise.all(listLoads);
process.stdout.write(
  `${JSON.stringify({
    ontology_list_requests: ontologyListRequests,
    non_knowledge_tree_requests: nonKnowledgeTreeRequests,
  })}\n`,
);
