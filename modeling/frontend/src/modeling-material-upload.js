export async function uploadModelingMaterials({ projectId, files, uploadFile }) {
  const selected = Array.from(files ?? []);
  const filenames = [];
  for (const file of selected) {
    await uploadFile(projectId, file);
    filenames.push(String(file?.name ?? '未命名资料'));
  }
  return { count: filenames.length, filenames };
}
