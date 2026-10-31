// Presentation only: retain source Markdown, branch checks, and pass-rate results.
const textOf = (node) => node.value ?? (node.children || []).map(textOf).join('');
const coverageMetric = /(?:设计|对象|逻辑|规则|指标|总体|平均)?覆盖[率度]\s*[：:]\s*\d/;
const branchCount = /^(?:判断分支总数|已覆盖分支数)\s*[：:]\s*\d/;
const metricLabel = /^(?:(?:设计|对象|逻辑|规则|指标|总体|平均)?覆盖[率度]|判断分支总数|已覆盖分支数)$/;

export function hideCoverageMetrics() {
  return function transform(tree) {
    function visit(parent) {
      if (!parent.children) return;
      parent.children = parent.children.filter((node) => {
        if (node.type === 'code' || node.type === 'inlineCode') return true;
        if (node.type === 'table') {
          // Tables may put labels in column headers or in a label/value row.
          const columns = new Set((node.children[0]?.children || [])
            .flatMap((cell, i) => metricLabel.test(textOf(cell).trim()) ? [i] : []));
          node.children = node.children.filter((row, i) => i === 0
            || !metricLabel.test(textOf(row.children?.[0] || {}).trim()));
          for (const row of node.children) {
            row.children = row.children.filter((_, i) => !columns.has(i));
          }
          if (node.align) node.align = node.align.filter((_, i) => !columns.has(i));
          if (!node.children[0]?.children.length || node.children.length === 1) return false;
        }
        const text = textOf(node).trim();
        if (node.type === 'heading' && /覆盖[率度]/.test(text)) {
          node.children = [{ type: 'text', value: text.replace(/覆盖[率度].*$/, '缺项检查') }];
        } else if (['paragraph', 'listItem', 'tableRow'].includes(node.type)
                   && (coverageMetric.test(text) || branchCount.test(text))) {
          // Keep missing-item details if a legacy line combined them with a metric.
          const missing = text.match(/(?:未覆盖分支|未覆盖|待补场景)[：:][\s\S]+/);
          if (!missing) return false;
          const children = [{ type: 'text', value: missing[0] }];
          node.children = node.type === 'listItem' ? [{ type: 'paragraph', children }]
            : node.type === 'tableRow' ? [{ type: 'tableCell', children }] : children;
        }
        visit(node);
        return true;
      });
    }
    visit(tree);
  };
}
