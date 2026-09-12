/**
 * Route table for the console.
 *
 * Screens are lazy-loaded so the map bundle - OpenLayers is the largest single
 * dependency - is not downloaded to reach the login form.
 */

import React, { Suspense, lazy } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import AppShell from './components/layout/AppShell.jsx';
import LoginPage from './pages/LoginPage.jsx';
import { Spinner } from './components/ui/index.jsx';
import { useAuth } from './lib/auth.jsx';

const DashboardPage = lazy(() => import('./pages/DashboardPage.jsx'));
const RegistryPage = lazy(() => import('./pages/RegistryPage.jsx'));
const CameraDetailPage = lazy(() => import('./pages/CameraDetailPage.jsx'));
const MapPage = lazy(() => import('./pages/MapPage.jsx'));
const GapAnalysisPage = lazy(() => import('./pages/GapAnalysisPage.jsx'));
const HealthPage = lazy(() => import('./pages/HealthPage.jsx'));
const VideoWallPage = lazy(() => import('./pages/VideoWallPage.jsx'));
const VehicleSearchPage = lazy(() => import('./pages/VehicleSearchPage.jsx'));
const AlertsPage = lazy(() => import('./pages/AlertsPage.jsx'));
const FederationPage = lazy(() => import('./pages/FederationPage.jsx'));
const PlaybackPage = lazy(() => import('./pages/PlaybackPage.jsx'));
const UsersPage = lazy(() => import('./pages/UsersPage.jsx'));
const AuditPage = lazy(() => import('./pages/AuditPage.jsx'));

function FullPageSpinner() {
  return (
    <div className="flex h-full items-center justify-center py-24">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

function RequireAuth({ children }) {
  const { isAuthenticated, status } = useAuth();
  const location = useLocation();

  if (status === 'loading') return <FullPageSpinner />;
  if (!isAuthenticated) {
    // `state.from` is what sends the user back where they were aiming once
    // they have signed in.
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return children;
}

function RequireRole({ role, children }) {
  const { atLeast } = useAuth();
  if (!atLeast(role)) {
    return (
      <div className="p-6">
        <div className="rounded-md border border-state-warn/40 bg-state-warn/10 p-4">
          <p className="text-sm font-medium text-state-warn">Not available to your role</p>
          <p className="mt-1 text-xs text-slate-300">
            This screen requires the {role} role. Your account does not hold it.
          </p>
        </div>
      </div>
    );
  }
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route
          index
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <DashboardPage />
            </Suspense>
          }
        />
        <Route
          path="registry"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <RegistryPage />
            </Suspense>
          }
        />
        <Route
          path="registry/:cameraId"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <CameraDetailPage />
            </Suspense>
          }
        />
        <Route
          path="map"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <MapPage />
            </Suspense>
          }
        />
        <Route
          path="reports/gap-analysis"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <GapAnalysisPage />
            </Suspense>
          }
        />
        <Route
          path="reports/health"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <HealthPage />
            </Suspense>
          }
        />
        <Route
          path="wall"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <VideoWallPage />
            </Suspense>
          }
        />
        <Route
          path="search/vehicles"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <VehicleSearchPage />
            </Suspense>
          }
        />
        <Route
          path="alerts"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <AlertsPage />
            </Suspense>
          }
        />
        <Route
          path="federation"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <FederationPage />
            </Suspense>
          }
        />
        <Route
          path="vms/playback"
          element={
            <Suspense fallback={<FullPageSpinner />}>
              <PlaybackPage />
            </Suspense>
          }
        />
        <Route
          path="admin/users"
          element={
            <RequireRole role="DEPT_ADMIN">
              <Suspense fallback={<FullPageSpinner />}>
                <UsersPage />
              </Suspense>
            </RequireRole>
          }
        />
        <Route
          path="admin/audit"
          element={
            <RequireRole role="DEPT_ADMIN">
              <Suspense fallback={<FullPageSpinner />}>
                <AuditPage />
              </Suspense>
            </RequireRole>
          }
        />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
