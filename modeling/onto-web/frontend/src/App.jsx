import { Button, Card, Empty, Input, List, Splitter, Typography, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import CenterPane from './components/CenterPane.jsx';
import ChatPane from './components/ChatPane.jsx';
import LeftPane from './components/LeftPane.jsx';
import { api } from './lib/api.js';

export default function App() {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(null);
  const [tree, setTree] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [newName, setNewName] = useState('');

  const refreshProjects = useCallback(() => {
    api.listProjects().then((data) => setProjects(data.projects)).catch(() => {});
  }, []);

  const refreshTree = useCallback(() => {
    if (!project) return;
    api.fileTree(project.id).then((data) => setTree(data.tree)).catch(() => {});
  }, [project]);

  useEffect(() => {
    refreshProjects();
  }, [refreshProjects]);

  useEffect(() => {
    refreshTree();
  }, [refreshTree]);

  const createProject = async () => {
    try {
      const created = await api.createProject(newName);
      setNewName('');
      refreshProjects();
      setProject(created);
    } catch (error) {
      message.error(error.message);
    }
  };

  if (!project) {
    return (
      <div className="project-picker">
        <Typography.Title level={3}>本体建模工作台</Typography.Title>
        <Card title="新建建模任务">
          <Input.Search
            placeholder="任务名称，如：电商订单建模"
            enterButton="创建"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onSearch={createProject}
          />
        </Card>
        <Card title="已有任务">
          {projects.length === 0 ? (
            <Empty description="暂无任务" />
          ) : (
            <List
              dataSource={projects}
              renderItem={(item) => (
                <List.Item
                  actions={[
                    <Button key="open" type="link" onClick={() => setProject(item)}>
                      打开
                    </Button>,
                  ]}
                >
                  <List.Item.Meta
                    title={item.name}
                    description={new Date(item.created_at * 1000).toLocaleString()}
                  />
                </List.Item>
              )}
            />
          )}
        </Card>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <span className="brand">本体建模工作台</span>
        <span className="project-name">{project.name}</span>
        <Button size="small" onClick={() => setProject(null)}>
          切换任务
        </Button>
      </div>
      <div className="app-body">
        <Splitter>
          <Splitter.Panel defaultSize="20%" min="12%" max="35%">
            <div className="pane pane-left">
              <LeftPane
                project={project}
                tree={tree}
                onSelectFile={setSelectedFile}
                onRefresh={refreshTree}
              />
            </div>
          </Splitter.Panel>
          <Splitter.Panel>
            <div className="pane pane-center">
              <CenterPane
                project={project}
                file={selectedFile}
                onOpenFile={setSelectedFile}
              />
            </div>
          </Splitter.Panel>
          <Splitter.Panel defaultSize="32%" min="24%" max="48%">
            <div className="pane pane-right">
              <ChatPane project={project} onWorkspaceChanged={refreshTree} />
            </div>
          </Splitter.Panel>
        </Splitter>
      </div>
    </div>
  );
}
