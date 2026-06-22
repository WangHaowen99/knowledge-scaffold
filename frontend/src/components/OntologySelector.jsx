import { useState, useEffect, useRef } from 'react';
import { ChevronDown } from 'lucide-react';

/**
 * 通用本体选择器组件
 * @param {string} ontologyId - 当前选中的本体ID
 * @param {Array} ontologyOptions - 本体列表 [{id, name}]
 * @param {Function} onOntologyIdChange - 切换回调
 * @param {Function} onBeforeChange - 切换前校验（如检查未保存更改），返回 boolean
 */
function OntologySelector({ 
  ontologyId, 
  ontologyOptions = [], 
  onOntologyIdChange,
  onBeforeChange
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [hoveredOntology, setHoveredOntology] = useState(null);
  const menuRef = useRef(null);

  const currentOntology = ontologyOptions.find((opt) => opt.id === ontologyId);
  const currentOntologyName = currentOntology ? (currentOntology.name || currentOntology.id) : '未选择本体';

  useEffect(() => {
    const handleClickOutside = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setIsOpen(false);
        setHoveredOntology(null);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleSelect = (id) => {
    if (id === ontologyId) {
      setIsOpen(false);
      setHoveredOntology(null);
      return;
    }
    
    const proceed = onBeforeChange ? onBeforeChange() : true;
    if (proceed) {
      onOntologyIdChange?.(id);
      setIsOpen(false);
      setHoveredOntology(null);
    }
  };

  const handleOptionHover = (event, opt, displayName, description) => {
    if (!description || !menuRef.current) {
      setHoveredOntology(null);
      return;
    }
    const itemRect = event.currentTarget.getBoundingClientRect();
    const containerRect = menuRef.current.getBoundingClientRect();
    setHoveredOntology({
      id: opt.id,
      name: displayName,
      description,
      top: Math.max(0, itemRect.top - containerRect.top),
    });
  };

  return (
    <div className="sops-ontology-select-group">
      <span className="sops-select-label">本体</span>
      <div className="sops-custom-select-container" ref={menuRef}>
        <div
          className={`sops-select-trigger ${isOpen ? 'is-open' : ''}`}
          onClick={() => setIsOpen(!isOpen)}
        >
          <span className="sops-select-text">{currentOntologyName}</span>
          <ChevronDown size={12} className={`sops-select-chevron ${isOpen ? 'rotated' : ''}`} />
        </div>

        {isOpen && (
          <>
            <div className="sops-select-dropdown-menu" onMouseLeave={() => setHoveredOntology(null)}>
              <div className="sops-dropdown-inner">
                {ontologyOptions.length === 0 ? (
                  <div className="sops-dropdown-item empty">暂无本体</div>
                ) : (
                  ontologyOptions.map((opt) => {
                    const displayName = opt.name || opt.id;
                    const isDisabled = opt.enabled === false;
                    const description = String(opt.description || '').trim();
                    return (
                      <div
                        key={opt.id}
                        className={`sops-dropdown-item ${ontologyId === opt.id ? 'active' : ''} ${isDisabled ? 'disabled-ontology' : ''}`}
                        onClick={() => handleSelect(opt.id)}
                        onMouseEnter={(event) => handleOptionHover(event, opt, displayName, description)}
                        onFocus={(event) => handleOptionHover(event, opt, displayName, description)}
                        title={description || displayName}
                        tabIndex={0}
                      >
                        <span className="sops-dropdown-item-name">
                          {displayName}
                          {isDisabled && <span className="sops-dropdown-status">已禁用</span>}
                        </span>
                        <span className="sops-dropdown-item-id">{opt.id}</span>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
            {hoveredOntology?.description && (
              <div
                className="sops-dropdown-hover-card"
                style={{ top: `${hoveredOntology.top}px` }}
              >
                <div className="sops-dropdown-hover-card-title">{hoveredOntology.name}</div>
                <div className="sops-dropdown-hover-card-id">{hoveredOntology.id}</div>
                <div className="sops-dropdown-hover-card-description">
                  {hoveredOntology.description}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default OntologySelector;
