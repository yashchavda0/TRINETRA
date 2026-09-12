import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, ShieldAlert, UserPlus, Users as UsersIcon } from 'lucide-react';

import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Spinner,
  Table,
  Td,
  Th,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';
import { ROLE_LABELS, useAuth } from '../lib/auth.jsx';

const DEPARTMENTS = ['POLICE', 'RTO', 'GSRTC', 'CIVIL_SUPPLIES', 'REVENUE', 'PRIVATE'];

export default function UsersPage() {
  const { user, atLeast, departmentScope } = useAuth();
  const queryClient = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [includeInactive, setIncludeInactive] = useState(false);
  const [form, setForm] = useState({
    email: '',
    full_name: '',
    password: '',
    role: 'VIEWER',
    department_id: departmentScope || '',
  });

  const users = useQuery({
    queryKey: ['users', includeInactive],
    queryFn: () => api.get('/api/v1/users', { include_inactive: includeInactive, limit: 200 }),
  });

  const create = useMutation({
    mutationFn: (payload) => api.post('/api/v1/users', payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      setShowCreate(false);
      setForm({
        email: '',
        full_name: '',
        password: '',
        role: 'VIEWER',
        department_id: departmentScope || '',
      });
    },
  });

  const update = useMutation({
    mutationFn: ({ id, changes }) => api.patch(`/api/v1/users/${id}`, changes),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['users'] }),
  });

  const set = (key) => (event) => setForm((f) => ({ ...f, [key]: event.target.value }));

  return (
    <div className="min-h-full">
      <PageHeader
        title="Users & roles"
        description={
          departmentScope
            ? `Accounts scoped to ${departmentScope}. You cannot see or create accounts in other departments.`
            : 'Every console account across the state.'
        }
        actions={
          <>
            <label className="flex items-center gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={includeInactive}
                onChange={(event) => setIncludeInactive(event.target.checked)}
                className="h-3.5 w-3.5 rounded border-ink-600 bg-ink-900"
              />
              Show deactivated
            </label>
            <Button variant="primary" size="sm" onClick={() => setShowCreate((open) => !open)}>
              <UserPlus className="h-3.5 w-3.5" /> New user
            </Button>
          </>
        }
      />

      <div className="space-y-4 p-4 sm:p-6">
        <div className="flex items-start gap-2 rounded-md border border-ink-700 bg-ink-850 px-3 py-2 text-xs text-slate-400">
          <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" aria-hidden />
          <p>
            Roles are enforced by the API, not by this screen. A department administrator can
            only act inside their own department, and only a state administrator can grant the
            state-administrator role.
          </p>
        </div>

        {showCreate && (
          <Card className="border-accent/40">
            <CardHeader title="Create user" description="The password is set once here and is never displayed again" />
            <form
              onSubmit={(event) => {
                event.preventDefault();
                const payload = { ...form };
                if (payload.role === 'SUPER_ADMIN') delete payload.department_id;
                create.mutate(payload);
              }}
            >
              <CardBody className="grid gap-3 sm:grid-cols-2">
                <Field label="Official email" required>
                  <Input type="email" required value={form.email} onChange={set('email')} />
                </Field>
                <Field label="Full name" required>
                  <Input required value={form.full_name} onChange={set('full_name')} />
                </Field>
                <Field label="Initial password" required hint="At least 8 characters">
                  <Input
                    type="password"
                    required
                    minLength={8}
                    value={form.password}
                    onChange={set('password')}
                  />
                </Field>
                <Field label="Role" required>
                  <Select value={form.role} onChange={set('role')}>
                    <option value="VIEWER">Viewer — read only</option>
                    <option value="OPERATOR">Operator — view, stream, acknowledge</option>
                    <option value="DEPT_ADMIN">Department administrator</option>
                    {atLeast('SUPER_ADMIN') && (
                      <option value="SUPER_ADMIN">State administrator</option>
                    )}
                  </Select>
                </Field>
                {form.role !== 'SUPER_ADMIN' && (
                  <Field label="Department" required>
                    <Select
                      value={form.department_id}
                      onChange={set('department_id')}
                      disabled={Boolean(departmentScope)}
                    >
                      <option value="">Select…</option>
                      {(departmentScope ? [departmentScope] : DEPARTMENTS).map((d) => (
                        <option key={d} value={d}>
                          {d}
                        </option>
                      ))}
                    </Select>
                  </Field>
                )}
                {create.isError && (
                  <div className="sm:col-span-2">
                    <ErrorState error={create.error} />
                  </div>
                )}
              </CardBody>
              <div className="flex justify-end gap-2 border-t border-ink-700 px-4 py-3">
                <Button variant="ghost" onClick={() => setShowCreate(false)}>
                  Cancel
                </Button>
                <Button type="submit" variant="primary" disabled={create.isPending}>
                  {create.isPending ? <Spinner className="h-3.5 w-3.5" /> : 'Create user'}
                </Button>
              </div>
            </form>
          </Card>
        )}

        <Card>
          {users.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : users.isError ? (
            <CardBody>
              <ErrorState error={users.error} onRetry={users.refetch} />
            </CardBody>
          ) : users.data?.items?.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Email</Th>
                  <Th>Role</Th>
                  <Th>Department</Th>
                  <Th>Last sign-in</Th>
                  <Th className="text-right">Actions</Th>
                </tr>
              </thead>
              <tbody>
                {users.data.items.map((row) => (
                  <tr key={row.id} className="hover:bg-ink-800/60">
                    <Td className="text-xs">
                      {row.full_name}
                      {row.id === user?.id && (
                        <span className="ml-2 text-[10px] text-slate-500">(you)</span>
                      )}
                    </Td>
                    <Td className="font-mono text-xs text-slate-400">{row.email}</Td>
                    <Td>
                      <Badge tone={row.role === 'SUPER_ADMIN' ? 'info' : 'neutral'}>
                        {ROLE_LABELS[row.role] || row.role}
                      </Badge>
                    </Td>
                    <Td className="text-xs">{row.department_id || 'All departments'}</Td>
                    <Td className="text-xs text-slate-400">
                      {row.last_login_at ? new Date(row.last_login_at).toLocaleString() : 'never'}
                    </Td>
                    <Td className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          title="Reset password"
                          onClick={() => {
                            const password = window.prompt(
                              `New password for ${row.email} (at least 8 characters):`,
                            );
                            if (password && password.length >= 8) {
                              update.mutate({ id: row.id, changes: { password } });
                            }
                          }}
                        >
                          <KeyRound className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          size="sm"
                          variant={row.is_active ? 'ghost' : 'secondary'}
                          disabled={row.id === user?.id}
                          title={row.id === user?.id ? 'You cannot deactivate yourself' : undefined}
                          onClick={() =>
                            update.mutate({
                              id: row.id,
                              changes: { is_active: !row.is_active },
                            })
                          }
                        >
                          {row.is_active ? 'Deactivate' : 'Reactivate'}
                        </Button>
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState
              icon={UsersIcon}
              title="No users yet"
              description="Create the first account for this department."
            />
          )}
        </Card>

        {update.isError && <ErrorState error={update.error} />}
      </div>
    </div>
  );
}
