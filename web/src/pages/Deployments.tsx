import { GitBranch, Rocket, ShieldAlert } from "lucide-react";
import { href } from "../shell/routes";
import { Card, EmptyState, LinkButton, PageHeader } from "../ui";

// Honest by design: the platform has no deployment adapter in v2.1 (ТЗ §2 puts autonomous
// production deployment out of scope). Nothing here pretends a deploy happened.
export default function Deployments() {
  return (
    <div className="page">
      <PageHeader
        icon={<Rocket size={18} />}
        title="Деплой и среды"
        subtitle="Local, Development, Staging, Production, self-hosted runners и GitHub Actions."
      />
      <Card>
        <EmptyState
          icon={<ShieldAlert size={20} />}
          title="Деплой не подключён"
          action={
            <div className="row">
              <LinkButton href={href("infrastructure")}>Инфраструктура платформы</LinkButton>
              <LinkButton href={href("tasks")} icon={<GitBranch size={14} />}>
                Ветки и PR задач
              </LinkButton>
            </div>
          }
        >
          В AI Coding Platform v2.1 нет адаптера деплоя: изменения доходят до проекта веткой или
          pull request, а выпуск делает ваш CI/CD (например, reusable workflow <code>release</code> в
          GitHub Actions). Автономный деплой в Production вне рамок v2.1 (ТЗ §2). Когда адаптер
          появится, здесь будут среды, версии, история и подтверждение Production-деплоя с проверкой
          прав — без вымышленных статусов до этого.
        </EmptyState>
      </Card>
    </div>
  );
}
