// 首登改密（ADR-0107）：账号还是初始密码或刚被管理员重置时，业务接口一律 403 password_change_required，
// 应用只渲染这一页，不进项目、不预取。改完后令牌全部作废，回登录页用新密码登录
import { useApp } from '../../app/context.js';
import { Button } from '../../ui/index.jsx';
import { PasswordForm } from '../me/PasswordForm.jsx';

export default function PasswordChangePage() {
  const { user, logout } = useApp();
  return (
    <div className="app">
      <section className="en-lg-r" style={{ flex: 1 }}>
        <div className="en-lg-card">
          <h2>请先修改初始密码</h2>
          <p className="muted mt8">账号 {user?.username}：完成修改后，请使用新密码重新登录。</p>
          <PasswordForm
            username={user?.username}
            aside={<Button kind="ghost" icon="logout" onClick={() => logout()}>退出登录</Button>}
          />
        </div>
      </section>
    </div>
  );
}
