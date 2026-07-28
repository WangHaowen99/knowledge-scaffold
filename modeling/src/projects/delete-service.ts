import { deleteProject } from './repo.js';
import { deleteProjectGraphImportUploadFiles } from '../graph-import/upload-store.js';

export function deleteProjectLocalData(projectId: string, owner: string): boolean {
  deleteProjectGraphImportUploadFiles({ projectId, owner });
  return deleteProject(projectId, owner);
}
