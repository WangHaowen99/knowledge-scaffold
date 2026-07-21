function invalidManifestId() {
  throw new Error('最新知识包 manifest.yaml 中缺少有效 id');
}

export function parseKnowledgePackageManifestId(manifest) {
  if (typeof manifest !== 'string') invalidManifestId();
  const match = /^id:[ \t]*(.*?)[ \t]*$/m.exec(manifest);
  const scalar = match?.[1]?.trim();
  if (!scalar) invalidManifestId();

  if (scalar.startsWith('"')) {
    try {
      const value = JSON.parse(scalar);
      if (typeof value === 'string' && value) return value;
    } catch {
      invalidManifestId();
    }
    invalidManifestId();
  }

  if (scalar.startsWith("'") && scalar.endsWith("'")) {
    const value = scalar.slice(1, -1).replace(/''/g, "'");
    if (value) return value;
    invalidManifestId();
  }

  return scalar;
}

export function latestKnowledgePackageManifestId(packageFile) {
  const manifest = typeof packageFile?.rendered === 'string'
    ? packageFile.rendered
    : packageFile?.content;
  return parseKnowledgePackageManifestId(manifest);
}

export async function loadLatestKnowledgePackageManifestId(projectId, getPackageFile) {
  const packageFile = await getPackageFile(projectId, 'manifest.yaml');
  return latestKnowledgePackageManifestId(packageFile);
}
