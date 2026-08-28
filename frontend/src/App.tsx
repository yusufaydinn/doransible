import { Link, NavLink, Route, Routes } from "react-router-dom";

import { HomePage } from "./pages/HomePage";
import { InventoryDetailPage } from "./pages/InventoryDetailPage";
import { InventoryListPage } from "./pages/InventoryListPage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { JobListPage } from "./pages/JobListPage";
import { NewInventoryPage } from "./pages/NewInventoryPage";
import { NewProjectPage } from "./pages/NewProjectPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectListPage } from "./pages/ProjectListPage";

export function App() {
  return (
    <div className="layout">
      <header className="layout__header">
        <Link to="/" className="brand">
          <span className="brand__mark" aria-hidden="true">
            D
          </span>
          <div className="brand__text">
            <h1 className="brand__name">DORAnsible</h1>
            <span className="brand__tagline">Deploy · Orchestrate · Report</span>
          </div>
        </Link>
        <nav aria-label="Ana gezinme">
          <NavLink to="/" end>
            Genel bakış
          </NavLink>
          <NavLink to="/projects">Project'ler</NavLink>
          <NavLink to="/inventories">Inventory'ler</NavLink>
          <NavLink to="/jobs">Çalıştırmalar</NavLink>
        </nav>
      </header>

      <main className="layout__main">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/projects" element={<ProjectListPage />} />
          <Route path="/projects/new" element={<NewProjectPage />} />
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          <Route path="/inventories" element={<InventoryListPage />} />
          <Route path="/inventories/new" element={<NewInventoryPage />} />
          <Route path="/inventories/:inventoryId" element={<InventoryDetailPage />} />
          <Route path="/jobs" element={<JobListPage />} />
          <Route path="/jobs/:jobId" element={<JobDetailPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
    </div>
  );
}
