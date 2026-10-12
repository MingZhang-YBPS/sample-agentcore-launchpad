import { Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import {
  api,
  errorMessage,
  type ResourceMapping,
  type ResourceMappingKind,
} from "../../../lib/api";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Field, Select, Table, Tag } from "../../ui";

const KINDS: ResourceMappingKind[] = ["kb", "memory", "gateway", "mcp_record", "skill"];

/**
 * Logical resource mapping for one workspace (T23): the table a promotion reads to
 * turn "this agent uses `kb:hr-policy`" into the id that exists HERE. Reading is open
 * to every member; editing needs the approval permission, the same one that accepts a
 * release into this environment. A row is edited by saving the same kind + name again.
 */
export function MappingsCard({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { can } = useAuth();
  const canEdit = can("promotion.approve");
  const [rows, setRows] = useState<ResourceMapping[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<ResourceMappingKind>("kb");
  const [name, setName] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.listResourceMappings(workspaceId);
      setRows(result.mappings);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    setBusy(true);
    try {
      await api.putResourceMapping(workspaceId, kind, name.trim(), {
        resource_id: resourceId.trim(),
        note: note.trim() || null,
      });
      toast("success", t("v2.workspaces.mappings.saved", { key: `${kind}:${name.trim()}` }));
      setName("");
      setResourceId("");
      setNote("");
      await load();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: ResourceMapping) => {
    setBusy(true);
    try {
      await api.deleteResourceMapping(workspaceId, row.kind, row.name);
      toast("success", t("v2.workspaces.mappings.removed", { key: row.key }));
      await load();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<ResourceMapping>[] = [
    { key: "kind", title: t("v2.workspaces.mappings.colKind"), render: (r) => <Tag tone="gray">{t(`v2.workspaces.mappings.kind.${r.kind}`)}</Tag> },
    { key: "name", title: t("v2.workspaces.mappings.colName"), render: (r) => <b className="mono">{r.key}</b> },
    { key: "id", title: t("v2.workspaces.mappings.colId"), render: (r) => <span className="mono">{r.resource_id}</span> },
    { key: "note", title: t("v2.workspaces.mappings.colNote"), render: (r) => <span className="v2-muted">{r.note || "—"}</span> },
    {
      key: "act",
      title: "",
      className: "right",
      render: (r) =>
        canEdit ? (
          <>
            <Button
              size="sm"
              onClick={() => {
                setKind(r.kind);
                setName(r.name);
                setResourceId(r.resource_id);
                setNote(r.note ?? "");
              }}
              testId={`v2-ws-mapping-edit-${r.key}`}
            >
              {t("v2.workspaces.mappings.edit")}
            </Button>{" "}
            <Button
              size="sm"
              kind="danger"
              disabled={busy}
              title={t("v2.workspaces.mappings.remove")}
              onClick={() => void remove(r)}
              testId={`v2-ws-mapping-delete-${r.key}`}
            >
              <Trash2 size={13} aria-hidden="true" />
            </Button>
          </>
        ) : null,
    },
  ];

  const ready = name.trim() !== "" && resourceId.trim() !== "";
  return (
    <Card
      title={t("v2.workspaces.mappings.title")}
      sub={t("v2.workspaces.mappings.sub", { count: rows.length })}
      testId="v2-ws-mappings"
    >
      <Alert>{t("v2.workspaces.mappings.hint")}</Alert>
      <Table
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={loading}
        error={error}
        onRetry={() => void load()}
        empty={t("v2.workspaces.mappings.empty")}
        testId="v2-ws-mappings-table"
      />
      {canEdit ? (
        <div className="v2-toolbar" data-testid="v2-ws-mapping-form">
          <Field label={t("v2.workspaces.mappings.colKind")}>
            <Select
              value={kind}
              options={KINDS.map((k) => ({ value: k, label: t(`v2.workspaces.mappings.kind.${k}`) }))}
              onChange={(v) => setKind(v as ResourceMappingKind)}
              testId="v2-ws-mapping-kind"
            />
          </Field>
          <Field label={t("v2.workspaces.mappings.logicalName")} hint={t("v2.workspaces.mappings.nameHint")}>
            <input
              className="v2-input mono"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="hr-policy"
              data-testid="v2-ws-mapping-name"
            />
          </Field>
          <Field label={t("v2.workspaces.mappings.colId")}>
            <input
              className="v2-input mono"
              value={resourceId}
              onChange={(e) => setResourceId(e.target.value)}
              data-testid="v2-ws-mapping-id"
            />
          </Field>
          <Field label={t("v2.workspaces.mappings.colNote")}>
            <input className="v2-input" value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
          <div className="end">
            <Button kind="primary" disabled={!ready || busy} onClick={() => void save()} testId="v2-ws-mapping-save">
              {t("v2.workspaces.mappings.save")}
            </Button>
          </div>
        </div>
      ) : (
        <Alert>{t("v2.workspaces.mappings.readOnly")}</Alert>
      )}
    </Card>
  );
}
