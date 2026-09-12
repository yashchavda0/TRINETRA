import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import App from './App.jsx';
import { AuthProvider } from './lib/auth.jsx';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A control room leaves screens open for hours; refetching on every
      // window focus would hammer the API for data that changes on its own
      // schedule. Screens that need live data set their own refetchInterval.
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: (failureCount, error) => {
        // 401 is handled by the API client (refresh, then sign-out) and 4xx
        // will not succeed on a retry - only retry genuine transport faults.
        if (error?.status >= 400 && error?.status < 500) return false;
        return failureCount < 2;
      },
    },
  },
});

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root is missing from index.html');
}

createRoot(container).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
