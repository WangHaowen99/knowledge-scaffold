import XLSX from 'xlsx';

export interface TermTableTemplate {
  buffer: Buffer;
  filename: string;
  contentType: string;
}

export function buildTermTableTemplate(): TermTableTemplate {
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([
    ['STATUS_CODE', 'STATUS_NAME', 'STATUS_ALIAS', 'STATUS_EN_NAME'],
    ['A', '启用', '有效', 'Active'],
    ['I', '停用', '无效', 'Inactive'],
  ]), 'status_terms');
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([
    ['CATEGORY_CODE', 'CATEGORY_NAME', 'CATEGORY_ALIAS'],
    ['C01', '类别一', '第一类'],
    ['C02', '类别二', '第二类'],
  ]), 'category_terms');
  return {
    buffer: Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' })),
    filename: 'term-table-template.xlsx',
    contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  };
}
