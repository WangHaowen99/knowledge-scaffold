import type { PackageSpec, Project } from '../../src/types.js';
import { renderPackage } from '../../src/workflow/package-render.js';

type M3Repo = typeof import('../../src/workflow/m3-repo.js');

export const neo4jConfig = (overrides: Record<string, unknown> = {}) => ({
  uri: 'bolt://neo4j:7687', username: 'neo4j', password: 'secret', database: 'neo4j',
  workspace_property: 'd2o_workspace_id', workspace_value: 'test',
  ontology_property: 'd2o_ontology_id', ontology_value: 'ontology-001',
  ...overrides,
});

export const neo4jInput = (overrides: Record<string, unknown> = {}) => ({
  mode: 'connection' as const,
  provider: 'neo4j' as const,
  config: neo4jConfig(overrides),
});

export const nl2sqlInput = (overrides: Record<string, unknown> = {}) => ({
  mode: 'connection' as const,
  provider: 'nl2sql_template' as const,
  config: {
    url: 'http://nl2sql:8080', token: 'token-value', scene: 'evaluation',
    evaluation_workspace_id: 'nl2sql-eval',
    ...overrides,
  },
});

export const jsonResponse = (body: unknown, status = 200) => new Response(
  JSON.stringify(body),
  { status },
);

export async function createReadyEvaluationProject(options: {
  name: string;
  ontologyId?: string;
  workspaceId?: string;
  storedOntologyId?: string;
  includeOntologyId?: boolean;
}): Promise<{
  project: Project;
  m3repo: M3Repo;
  packageSnapshot: {
    version: string;
    spec: PackageSpec;
    files: Record<string, string>;
    revisionOid: string;
  };
}> {
  const [projects, packages, m3repo, dataSources] = await Promise.all([
    import('../../src/projects/repo.js'),
    import('../../src/workflow/m2-repo.js'),
    import('../../src/workflow/m3-repo.js'),
    import('../../src/evaluation/data-source-repo.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: options.name });
  const ontologyId = options.ontologyId ?? `${project.id}-ontology`;
  const workspaceId = options.workspaceId ?? 'test';
  const spec: PackageSpec = {
    id: ontologyId, name: options.name, description: options.name, version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'graph', objects: [], operations: [],
  };
  packages.insertPackageVersion(project.id, spec.version, spec, { covered: [], uncovered: [] });
  m3repo.addTestcase(project.id, '查询测试数据', '标准答案');
  dataSources.saveProjectDataSource({
    projectId: project.id, mode: 'connection', provider: 'neo4j', workspaceId,
    ontologyId: options.includeOntologyId === false
      ? undefined
      : options.storedOntologyId ?? ontologyId,
    config: neo4jConfig({
      password: undefined,
      has_password: true,
      workspace_value: workspaceId,
      ontology_value: ontologyId,
    }),
  });
  return {
    project,
    m3repo,
    packageSnapshot: {
      version: spec.version,
      spec,
      files: renderPackage(spec),
      revisionOid: `test-revision-${project.id}`,
    },
  };
}
