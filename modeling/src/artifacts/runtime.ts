import { join } from 'node:path';

import { config } from '../config.js';
import { IsomorphicGitArtifactRepository } from './isomorphic-git-repository.js';
import { PackageArtifactStore } from './package-store.js';

let store: PackageArtifactStore | undefined;

export function getPackageArtifactStore(): PackageArtifactStore {
  store ??= new PackageArtifactStore({
    repository: new IsomorphicGitArtifactRepository({
      rootDir: join(config.dataDir, 'artifacts'),
    }),
  });
  return store;
}
