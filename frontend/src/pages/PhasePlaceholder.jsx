import React from 'react';
import { Link } from 'react-router-dom';
import { Construction } from 'lucide-react';

import { PageHeader } from '../components/layout/AppShell.jsx';
import { Badge, Card, CardBody, CardHeader } from '../components/ui/index.jsx';

/**
 * A screen whose backend has not been built yet.
 *
 * Deliberately not a mock: a fake video wall or an invented alert list would
 * read as working software to anyone evaluating this console, which is the one
 * thing a status screen must never do. It states what exists, what does not,
 * and what has to be built for the screen to become real.
 */
export default function PhasePlaceholder({
  title,
  model,
  description,
  planned,
  dependencies,
  available,
}) {
  return (
    <div className="min-h-full">
      <PageHeader
        title={title}
        description={description}
        actions={<Badge tone="warn">{model} · not yet implemented</Badge>}
      />

      <div className="space-y-4 p-4 sm:p-6">
        <Card className="border-state-warn/30">
          <CardBody className="flex items-start gap-3">
            <Construction className="mt-0.5 h-5 w-5 shrink-0 text-state-warn" aria-hidden />
            <div>
              <p className="text-sm font-medium text-slate-200">
                This screen is not built yet
              </p>
              <p className="mt-1 text-xs text-slate-400">
                It is listed in the navigation so the console's full scope is visible, but there
                is no backend behind it. Nothing on this page is simulated.
              </p>
            </div>
          </CardBody>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader title="What this screen will do" />
            <CardBody>
              <ul className="space-y-2">
                {planned.map((item) => (
                  <li key={item} className="flex gap-2 text-xs text-slate-300">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent" />
                    {item}
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="What it needs first" />
            <CardBody>
              <ul className="space-y-2">
                {dependencies.map((item) => (
                  <li key={item} className="flex gap-2 text-xs text-slate-300">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-state-warn" />
                    {item}
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
        </div>

        {available?.length > 0 && (
          <Card>
            <CardHeader title="Working today" description="Parts of this capability that already run" />
            <CardBody>
              <ul className="space-y-2">
                {available.map((item) => (
                  <li key={item.label} className="flex items-start gap-2 text-xs">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-state-up" />
                    <span className="text-slate-300">
                      {item.to ? (
                        <Link to={item.to} className="text-accent hover:underline">
                          {item.label}
                        </Link>
                      ) : (
                        item.label
                      )}
                      {item.note && <span className="text-slate-500"> — {item.note}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
        )}
      </div>
    </div>
  );
}
