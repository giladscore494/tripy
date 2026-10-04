import { BrowserRouter, Route, Routes } from "react-router-dom";

import { AuthProvider, useAuth } from "./auth/AuthProvider";
import { WorkspaceDataProvider } from "./hooks/WorkspaceData";
import { AppShell } from "./layouts/AppShell";
import { DashboardPage } from "./pages/DashboardPage";
import { DiagnosticsPage } from "./pages/DiagnosticsPage";
import { NewResearchPage } from "./pages/NewResearchPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { SeriesListPage, SeriesPage } from "./pages/SeriesPages";
import { SettingsPage } from "./pages/SettingsPage";
import { CheckingScreen, ServerProblemScreen, UnlockPage } from "./pages/UnlockPage";

export function WorkspaceRoutes() {
  return (
    <Routes>
      <Route path="/" element={<DashboardPage />} />
      <Route path="/research/new" element={<NewResearchPage />} />
      <Route path="/runs" element={<RunsPage />} />
      <Route path="/runs/:runId" element={<RunPage />} />
      <Route path="/series" element={<SeriesListPage />} />
      <Route path="/series/:seriesId" element={<SeriesPage />} />
      <Route path="/diagnostics" element={<DiagnosticsPage />} />
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}

export function Gate() {
  const { status, serverMessage, retry } = useAuth();
  if (status === "checking") return <CheckingScreen />;
  if (status === "locked") return <UnlockPage />;
  if (status === "misconfigured" || status === "unreachable") {
    return <ServerProblemScreen kind={status} message={serverMessage} onRetry={retry} />;
  }
  return (
    <WorkspaceDataProvider>
      <AppShell>
        <WorkspaceRoutes />
      </AppShell>
    </WorkspaceDataProvider>
  );
}

// Client-side routes (BrowserRouter): PR #3 serves dist/index.html from FastAPI for every non-API path, so a browser
// refresh on /runs/<id> or /series/<id> lands back here.
export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </BrowserRouter>
  );
}
