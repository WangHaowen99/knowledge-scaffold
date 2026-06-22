export const ONTOLOGY_IMPORT_DISCLAIMER = {
  title: '确认上传本体',
  inline:
    '安全提示：系统会按照已上传并启用的本体进行检索、推理和执行。上传前请确认本体来源可信、内容正确、权限合规，且其中的逻辑、脚本和配置不会造成误操作、数据泄露或安全风险。',
  body:
    '上传后，系统将以该本体作为运行依据，并会严格按照本体中的对象定义、流程逻辑和脚本声明执行。请确认该本体来源可信、内容正确、权限合规。因本体内容错误、恶意脚本或配置不当导致的业务结果、数据访问、执行风险或安全风险，由上传者自行负责。',
  acknowledgement: '我已确认本体来源可信、内容正确，并理解系统将按照该本体执行。',
};

export function canSubmitOntologyImport({ acknowledged } = {}) {
  return acknowledged === true;
}
