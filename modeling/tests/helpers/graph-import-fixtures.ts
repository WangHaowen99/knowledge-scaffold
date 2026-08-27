import * as XLSX from 'xlsx';
import type { PackageSpec, Project } from '../../src/types.js';
import type { GraphImportMapping, GraphImportPackageSchema } from '../../src/graph-import/types.js';

export function workbookBuffer(sheets: Record<string, unknown[][]>): Buffer {
  const workbook = XLSX.utils.book_new();
  for (const [name, rows] of Object.entries(sheets)) {
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(rows), name);
  }
  return Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));
}

export const graphSchema: GraphImportPackageSchema = {
  packageVersion: '1.0.0',
  objects: [
    {
      key: 'Part', title: '零件', label: '设备零件', defaultSheet: 'Part',
      propertyMappings: [
        { property: 'part_id', sourceColumn: 'part_id' },
        { property: 'relation_code', sourceColumn: 'relation_code' },
        { property: 'name', sourceColumn: 'name' },
      ],
    },
    {
      key: 'BOMItem', title: 'BOM 行', label: 'BOM 行', defaultSheet: 'BOMItem',
      propertyMappings: [
        { property: 'item_id', sourceColumn: 'item_id' },
        { property: 'parent_code', sourceColumn: 'parent_code' },
        { property: 'name', sourceColumn: 'name' },
      ],
    },
  ],
  valueBindings: [],
  relations: [{
    id: 'Part|contains|BOMItem|0', sourceObject: 'Part', relationName: 'contains',
    targetObject: 'BOMItem', note: '',
  }],
};

export const graphMapping: GraphImportMapping = {
  relations: [{
    sourceObject: 'Part', relationName: 'contains', targetObject: 'BOMItem',
    matchMode: 'field_match',
    sourceJoinProperty: 'relation_code', targetJoinProperty: 'parent_code',
  }],
};

export const graphPackageSpec: PackageSpec = {
  id: 'graph-demo', name: '图导入', description: '图导入', version: '1.0.0',
  language: 'zh-CN', defaultProviderAlias: 'graph',
  objects: [
    {
      key: 'Part', title: '零件', description: '零件', sourceTable: 'Part',
      primaryKey: ['part_id'],
      fields: [
        { key: 'part_id', sourceColumn: 'part_id' },
        { key: 'relation_code', sourceColumn: 'relation_code' },
        { key: 'name', sourceColumn: 'name' },
      ],
    },
    {
      key: 'BOMItem', title: 'BOM 行', description: 'BOM 行', sourceTable: 'BOMItem',
      primaryKey: ['item_id'],
      fields: [
        { key: 'item_id', sourceColumn: 'item_id' },
        { key: 'parent_code', sourceColumn: 'parent_code' },
        { key: 'name', sourceColumn: 'name' },
      ],
    },
  ],
  operations: [],
};

export const neo4jConnection = (overrides: Record<string, string> = {}) => ({
  uri: 'bolt://127.0.0.1:7687', username: 'neo4j', password: 'secret', database: 'neo4j',
  workspaceProperty: 'd2o_workspace_id', workspaceValue: 'workspace-1',
  ontologyProperty: 'd2o_ontology_id', ontologyValue: 'ontology-1',
  ...overrides,
});

export function recordingDriver(
  run?: (query: string, params: Record<string, unknown>) => Promise<unknown> | unknown,
) {
  const calls: Array<{ query: string; params: Record<string, unknown> }> = [];
  const state = {
    databases: [] as string[], sessionClosed: false, driverClosed: false, transactionCount: 0,
    nodeCount: 0, relationshipCount: 0,
  };
  const simulatedRun = (query: string, params: Record<string, unknown>) => {
    if (/RETURN nodeCount, relationshipCount/.test(query)) {
      return {
        records: [{
          get: (key: string) => (key === 'nodeCount' ? state.nodeCount : state.relationshipCount),
        }],
      };
    }
    if (/DELETE relationship/.test(query)) state.relationshipCount = 0;
    else if (/DELETE node/.test(query)) state.nodeCount = 0;
    else if (/MERGE \(node:/.test(query)) state.nodeCount += (params.rows as unknown[]).length;
    else if (/MERGE \(source\)-\[relationship:/.test(query)) {
      state.relationshipCount += (params.rows as unknown[]).length;
    }
    return {};
  };
  return {
    calls, state,
    factory: async () => ({
      session({ database }: { database: string }) {
        state.databases.push(database);
        return {
          async executeWrite(work: (transaction: any) => Promise<unknown>) {
            state.transactionCount += 1;
            return work({
              async run(query, params) {
                calls.push({ query, params });
                const result = await run?.(query, params);
                return result === undefined ? simulatedRun(query, params) : result;
              },
            });
          },
          async close() { state.sessionClosed = true; },
        };
      },
      async close() { state.driverClosed = true; },
    }),
  };
}

export async function createGraphImportProject(
  name: string,
  spec: PackageSpec = graphPackageSpec,
): Promise<Project> {
  const [projects, packages, dataSources] = await Promise.all([
    import('../../src/projects/repo.js'),
    import('../../src/workflow/m2-repo.js'),
    import('../../src/evaluation/data-source-repo.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name });
  packages.insertPackageVersion(project.id, spec.version, spec, {
    covered: [], uncovered: [],
  });
  dataSources.saveProjectDataSource({
    projectId: project.id, mode: 'connection', provider: 'neo4j',
    workspaceId: 'shadow-workspace', ontologyId: 'ontology-001',
    config: {
      uri: 'bolt://neo4j:7687', username: 'neo4j', database: 'domain', has_password: true,
      workspace_property: 'd2o_workspace_id', workspace_value: 'workspace-001',
      ontology_property: 'd2o_ontology_id', ontology_value: 'ontology-001',
    },
  });
  return project;
}
