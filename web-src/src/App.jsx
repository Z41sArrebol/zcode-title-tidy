import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Message,
  Modal,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from '@arco-design/web-react';

const { Text, Title } = Typography;
const { TabPane } = Tabs;

const FILTER_OPTIONS = [
  { label: '全部', value: 'all' },
  { label: '只看有问题的', value: 'problem' },
  { label: '只看未生成', value: 'pending' },
  { label: '只看改过的', value: 'changed' },
  { label: '只看手动改过的', value: 'custom' },
];

const SOURCE_COLOR = {
  generated: 'green',
  first_input: 'orange',
  custom: 'blue',
  default: 'gray',
};

const SOURCE_LABEL = {
  generated: '自动生成',
  first_input: '首条截断',
  custom: '手动',
  default: '默认',
};

const STATUS_LABEL = {
  pending: '待生成',
  ok: '已生成',
  fallback: '兜底',
  edited: '已修改',
  applied: '已应用',
};

const STATUS_TYPE = {
  ok: 'success',
  fallback: 'warning',
  pending: 'secondary',
  applied: 'success',
};

function splitTime(ms) {
  if (!ms) return ['—', ''];
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return [`${p(d.getMonth() + 1)}-${p(d.getDate())}`, `${p(d.getHours())}:${p(d.getMinutes())}`];
}

function wsLabel(name) {
  return name === 'default' ? '默认工作区' : name;
}

let csrfToken = '';

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify(body ?? {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && data.ok === undefined) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [stats, setStats] = useState({
    total: 0,
    generated: 0,
    selected: 0,
    problems: 0,
    workspaces: [],
  });
  const [progress, setProgress] = useState({});
  const [zcodeRunning, setZcodeRunning] = useState(false);
  const [zcodeState, setZcodeState] = useState('stopped');
  const [reverting, setReverting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState('全部');
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [selectedKeys, setSelectedKeys] = useState([]);
  const [drafts, setDrafts] = useState({});
  const [tableY, setTableY] = useState(400);
  const [applyVisible, setApplyVisible] = useState(false);
  const [zcodeVisible, setZcodeVisible] = useState(false);
  const [successVisible, setSuccessVisible] = useState(false);
  const [lastApply, setLastApply] = useState(null);
  const [applying, setApplying] = useState(false);
  const [regeneratingId, setRegeneratingId] = useState(null);
  const [rescanning, setRescanning] = useState(false);
  const [modelConfigured, setModelConfigured] = useState(false);
  const [compatible, setCompatible] = useState(true);
  const [compatibilityError, setCompatibilityError] = useState('');
  const [settingsVisible, setSettingsVisible] = useState(false);
  const [settingsForm, setSettingsForm] = useState({
    api_url: '',
    model: '',
    api_key: '',
    clear_api_key: false,
    zcode_dir: '',
  });
  const [settingsMeta, setSettingsMeta] = useState({});
  const [settingsError, setSettingsError] = useState('');
  const [testingSettings, setTestingSettings] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);

  const knownIds = useRef(new Set());
  const draftsRef = useRef({});
  const errorShown = useRef(false);

  useEffect(() => {
    draftsRef.current = drafts;
  }, [drafts]);

  useEffect(() => {
    const update = () => setTableY(Math.max(320, window.innerHeight - 360));
    update();
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, []);

  const consumeState = useCallback((data) => {
    const list = data.sessions || [];
    setSessions(list);
    setStats(data.stats || {
      total: 0,
      generated: 0,
      selected: 0,
      problems: 0,
      workspaces: [],
    });
    setProgress(data.progress || {});
    csrfToken = data.csrf_token || csrfToken;
    setZcodeState(data.zcode_state || (data.zcode_running ? 'running' : 'stopped'));
    setZcodeRunning(data.zcode_state ? data.zcode_state !== 'stopped' : !!data.zcode_running);
    setModelConfigured(!!data.model_configured);
    setCompatible(data.compatible !== false);
    setCompatibilityError(data.compatibility_error || '');
    setLastApply(data.last_apply || null);
    setLoading(false);

    setSelectedKeys((prev) => {
      const ids = new Set(list.map((s) => s.id));
      if (knownIds.current.size === 0) {
        list.forEach((s) => knownIds.current.add(s.id));
        // 公开发布默认保护手动标题：需要改时由用户自己勾选
        return list
          .filter((s) => s.selected !== false && s.old_source !== 'custom')
          .map((s) => s.id);
      }
      const added = list.filter((s) => !knownIds.current.has(s.id)).map((s) => s.id);
      list.forEach((s) => knownIds.current.add(s.id));
      return [...prev.filter((id) => ids.has(id)), ...added];
    });
  }, []);

  const loadState = useCallback(async () => {
    try {
      const res = await fetch('/api/state');
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || '读取失败');
      consumeState(data);
      errorShown.current = false;
      return data;
    } catch (err) {
      if (!errorShown.current) {
        Message.error(`读取服务失败：${err.message}`);
        errorShown.current = true;
      }
      setLoading(false);
      return null;
    }
  }, [consumeState]);

  useEffect(() => {
    let alive = true;
    let timer;

    const tick = async () => {
      const data = await loadState();
      if (!alive) return;
      timer = setTimeout(tick, data?.progress?.running ? 1000 : 5000);
    };

    tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [loadState]);

  useEffect(() => {
    if (tab !== '全部' && !(stats.workspaces || []).some(([name]) => name === tab)) {
      setTab('全部');
    }
  }, [stats, tab]);

  const tabs = useMemo(
    () => [['全部', stats.total || 0], ...(stats.workspaces || [])],
    [stats]
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return sessions.filter((s) => {
      if (tab !== '全部' && s.workspace !== tab) return false;
      if (filter === 'problem' && !(s.flags || []).length) return false;
      if (filter === 'pending' && (s.new_title || '')) return false;
      if (filter === 'changed' && (!s.new_title || s.new_title === s.old_title)) return false;
      if (filter === 'custom' && s.old_source !== 'custom') return false;
      if (q) {
        const hay = [s.old_title, s.new_title, s.first_prompt, s.digest, s.workspace]
          .join('\n')
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [sessions, tab, filter, search]);

  const currentTitle = useCallback(
    (s) => (drafts[s.id] !== undefined ? drafts[s.id] : s.new_title || ''),
    [drafts]
  );

  const selectedSet = useMemo(() => new Set(selectedKeys), [selectedKeys]);
  const applicable = useMemo(
    () =>
      sessions.filter(
        (s) => selectedSet.has(s.id) && currentTitle(s).trim() && !s.stale
      ),
    [sessions, selectedSet, currentTitle]
  );
  const applicableCount = applicable.length;
  const staleCount = useMemo(
    () => sessions.filter((s) => s.stale).length,
    [sessions]
  );

  const saveTitle = useCallback(async (id, value) => {
    const v = (value || '').trim().replace(/\s+/g, ' ');
    setSessions((prev) =>
      prev.map((s) =>
        s.id === id ? { ...s, new_title: v, gen_status: v ? 'edited' : 'pending' } : s
      )
    );
    setDrafts((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      await post('/api/update', { id, new_title: v });
    } catch (err) {
      Message.error(`保存失败：${err.message}`);
    }
  }, []);

  const flushDrafts = useCallback(async () => {
    const entries = Object.entries(draftsRef.current);
    if (!entries.length) return;

    const updated = entries.map(([id, value]) => [
      id,
      (value || '').trim().replace(/\s+/g, ' '),
    ]);

    await Promise.all(
      updated.map(([id, value]) => post('/api/update', { id, new_title: value }))
    );

    setSessions((prev) =>
      prev.map((s) => {
        const pair = updated.find(([id]) => id === s.id);
        return pair
          ? { ...s, new_title: pair[1], gen_status: pair[1] ? 'edited' : 'pending' }
          : s;
      })
    );
    setDrafts({});
  }, []);

  const openApply = async () => {
    try {
      await flushDrafts();
      setApplyVisible(true);
    } catch (err) {
      Message.error(`保存标题失败：${err.message}`);
    }
  };

  const doApply = async () => {
    if (applying) return;
    const ids = applicable.map((s) => s.id);
    if (!ids.length) {
      setApplyVisible(false);
      Message.warning('没有可应用的标题');
      return;
    }

    setApplying(true);
    try {
      const res = await post('/api/apply', { ids, confirm: true });
      if (res.ok) {
        setApplyVisible(false);
        setZcodeVisible(false);
        setLastApply(res);
        setSuccessVisible(true);
        setDrafts({});
        Message.success(`已写回 ${res.applied} 个标题`);
        loadState();
        return;
      }
      if (res.error === 'zcode_running') {
        setApplyVisible(false);
        setZcodeVisible(true);
        return;
      }
      if (res.error === 'verify_failed') {
        setApplyVisible(false);
        setLastApply(res);
        setSuccessVisible(true);
        loadState();
        Message.error(res.message || '写入完成，但校验不一致');
        return;
      }
      if (res.error === 'conflict') {
        setApplyVisible(false);
        Message.warning(res.message || '没有写入任何标题');
        loadState();
        return;
      }
      Message.error(res.message || res.error || '应用失败');
    } catch (err) {
      Message.error(`应用失败：${err.message}`);
    } finally {
      setApplying(false);
    }
  };

  const revertLast = async () => {
    if (!lastApply?.backup || reverting) return;
    setReverting(true);
    try {
      const res = await post('/api/revert', {
        backup: lastApply.backup,
        confirm: true,
      });
      if (res.ok) {
        Message.success(`已撤销 ${res.restored?.length || 0} 条标题`);
        setSuccessVisible(false);
        setLastApply(null);
        loadState();
        return;
      }
      if (res.error === 'zcode_running') {
        Message.warning('撤销前请先完全退出 ZCode');
        return;
      }
      Message.error(res.message || res.error || '撤销失败');
    } catch (err) {
      Message.error(`撤销失败：${err.message}`);
    } finally {
      setReverting(false);
    }
  };

  const regenerate = async (id) => {
    setRegeneratingId(id);
    try {
      const res = await post('/api/regenerate', { id });
      if (res.ok && res.item) {
        setSessions((prev) => prev.map((s) => (s.id === id ? res.item : s)));
        setDrafts((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        Message.success('已换一个标题');
      } else {
        Message.error(res.message || res.error || '生成失败');
      }
    } catch (err) {
      Message.error(`生成失败：${err.message}`);
    } finally {
      setRegeneratingId(null);
    }
  };

  const generate = async (ids, force) => {
    if (!ids.length) {
      Message.warning('没有可生成的行');
      return;
    }
    try {
      const res = await post('/api/generate', { ids, force });
      if (!res.ok) {
        Message.error(res.error || '启动失败');
        return;
      }
      Message.success(`已加入生成队列：${ids.length} 条`);
      loadState();
    } catch (err) {
      Message.error(`启动失败：${err.message}`);
    }
  };

  const rescan = async () => {
    setRescanning(true);
    try {
      const res = await post('/api/rescan', {});
      if (res.ok) {
        consumeState(res);
        Message.success('已重新扫描');
      } else {
        Message.error(res.error || '扫描失败');
      }
    } catch (err) {
      Message.error(`扫描失败：${err.message}`);
    } finally {
      setRescanning(false);
    }
  };

  const savePlan = async () => {
    try {
      const res = await post('/api/save', {});
      if (res.ok) Message.success(`方案已保存：${res.path}`);
      else Message.error(res.error || '保存失败');
    } catch (err) {
      Message.error(`保存失败：${err.message}`);
    }
  };

  const openZcode = async () => {
    try {
      const res = await post('/api/open-zcode', {});
      if (res.ok) Message.success('正在打开 ZCode');
      else Message.error(res.message || res.error || '没找到 ZCode.exe');
    } catch (err) {
      Message.error(err.message);
    }
  };

  const columns = [
    {
      title: '时间',
      dataIndex: 'updated_at',
      width: 96,
      render: (_, record) => {
        const [date, clock] = splitTime(record.updated_at);
        return (
          <div className="time-cell">
            <Text type="secondary">{date}</Text>
            <Text type="secondary">{clock}</Text>
          </div>
        );
      },
    },
    {
      title: '现在的标题',
      dataIndex: 'old_title',
      render: (_, record) => (
        <div>
          <Tooltip content={record.old_title} position="top">
            <div className="old-title">{record.old_title || '（无标题）'}</div>
          </Tooltip>
          <div className="cell-meta">
            {tab === '全部' && (
              <Tag size="small" color="gray">{wsLabel(record.workspace)}</Tag>
            )}
            {record.stale && <Tag size="small" color="red">方案过期</Tag>}
            <Tag size="small" color={SOURCE_COLOR[record.old_source] || 'gray'}>
              {SOURCE_LABEL[record.old_source] || record.old_source}
            </Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {record.title_len || 0} 字
            </Text>
            {(record.flags || []).slice(0, 2).map((flag) => (
              <Tag
                key={flag}
                size="small"
                color={flag === '手动改过' || flag === '无用户消息' ? 'gray' : 'red'}
              >
                {flag}
              </Tag>
            ))}
            {(record.flags || []).length > 2 && (
              <Tooltip content={(record.flags || []).slice(2).join('、')}>
                <Tag size="small" color="gray">
                  +{record.flags.length - 2}
                </Tag>
              </Tooltip>
            )}
          </div>
        </div>
      ),
    },
    {
      title: '新标题',
      dataIndex: 'new_title',
      width: 420,
      render: (_, record) => {
        const value =
          drafts[record.id] !== undefined ? drafts[record.id] : record.new_title || '';
        const len = [...value].length;
        return (
          <div>
            <Input
              value={value}
              placeholder="尚未生成"
              allowClear
              onChange={(v) => setDrafts((prev) => ({ ...prev, [record.id]: v }))}
              onBlur={(e) => saveTitle(record.id, e.target.value)}
              onPressEnter={(e) => saveTitle(record.id, e.target.value)}
            />
            <div className="new-meta">
              <Text
                type="secondary"
                style={{ fontSize: 12, color: len > 30 ? '#f53f3f' : undefined }}
              >
                {value ? `${len} 字` : '—'}
              </Text>
              <Text
                type={STATUS_TYPE[record.gen_status] || undefined}
                style={{ fontSize: 12 }}
              >
                {STATUS_LABEL[record.gen_status] || record.gen_status}
              </Text>
            </div>
          </div>
        );
      },
    },
    {
      title: '操作',
      width: 88,
      render: (_, record) => (
        <Button
          size="mini"
          type="text"
          loading={regeneratingId === record.id}
          disabled={!modelConfigured || !compatible}
          onClick={() => regenerate(record.id)}
        >
          换一个
        </Button>
      ),
    },
  ];

  const openSettings = async () => {
    try {
      const res = await fetch('/api/settings');
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || '读取设置失败');
      const s = data.settings || {};
      setSettingsMeta(s);
      setSettingsForm({
        api_url: s.api_url || '',
        model: s.model || '',
        api_key: '',
        clear_api_key: false,
        zcode_dir: s.zcode_dir || '',
      });
      setSettingsError('');
      setSettingsVisible(true);
    } catch (err) {
      Message.error(`读取设置失败：${err.message}`);
    }
  };

  const updateSetting = (key, value) => {
    setSettingsForm((prev) => ({ ...prev, [key]: value }));
  };

  const testConnection = async () => {
    setTestingSettings(true);
    setSettingsError('');
    try {
      const res = await post('/api/settings/test', {
        api_url: settingsForm.api_url,
        model: settingsForm.model,
        api_key: settingsForm.api_key || undefined,
      });
      if (res.ok) {
        Message.success(`连接成功，${res.latency_ms} ms`);
      } else {
        setSettingsError(res.message || res.error || '连接失败');
      }
    } catch (err) {
      setSettingsError(`连接失败：${err.message}`);
    } finally {
      setTestingSettings(false);
    }
  };

  const saveSettings = async () => {
    setSavingSettings(true);
    setSettingsError('');
    try {
      const res = await post('/api/settings', {
        api_url: settingsForm.api_url,
        model: settingsForm.model,
        api_key: settingsForm.api_key,
        clear_api_key: settingsForm.clear_api_key,
        zcode_dir: settingsForm.zcode_dir,
      });
      if (!res.ok) {
        setSettingsError(res.message || res.error || '保存失败');
        return;
      }
      if (res.state) consumeState(res.state);
      setSettingsVisible(false);
      Message.success('设置已保存');
    } catch (err) {
      setSettingsError(`保存失败：${err.message}`);
    } finally {
      setSavingSettings(false);
    }
  };

  return (
    <div className="app">
      <div className="panel header-panel">
        <div>
          <Title heading={4} className="header-title">
            ZCode 标题整理
          </Title>
          <Text type="secondary" className="header-sub">
            把长短不一的会话标题统一成短标题。写回前会备份两个数据库。
          </Text>
        </div>
        <div className="header-stats">
          <Text type="secondary">
            已生成 {stats.generated} / {stats.total}
          </Text>
          <Text style={{ color: stats.problems ? '#f53f3f' : undefined }}>
            有问题 {stats.problems}
          </Text>
          <Space>
            <Button onClick={openSettings}>设置</Button>
            <Button onClick={rescan} loading={rescanning}>
              重新扫描
            </Button>
          </Space>
        </div>
      </div>

      {!compatible && (
        <Alert
          type="error"
          showIcon
          content={`数据库结构不兼容：${compatibilityError || '未知原因'}。请到设置里检查 ZCode 数据目录。`}
          style={{ marginTop: 12 }}
        />
      )}

      {compatible && !modelConfigured && (
        <Alert
          type="info"
          showIcon
          content="还没有配置模型服务，自动生成不可用；可以手动编辑标题，或点右上角「设置」配置。"
          style={{ marginTop: 12 }}
        />
      )}

      {zcodeRunning && (
        <Alert
          type="warning"
          showIcon
          content={
            zcodeState === 'unknown'
              ? '无法确认 ZCode 是否已退出。应用前请确认它已完全关闭（包括托盘）。'
              : 'ZCode 正在运行。应用标题前请完全退出 ZCode（包括托盘）。'
          }
          style={{ marginTop: 12 }}
        />
      )}

      {staleCount > 0 && (
        <Alert
          type="warning"
          showIcon
          content={`有 ${staleCount} 条方案已过期（生成后原标题被改过），应用时会跳过；请重新扫描并重新生成这些行。`}
          style={{ marginTop: 12 }}
        />
      )}

      <div className="panel toolbar-panel">
        <div className="toolbar-row">
          <Tabs
            activeTab={tab}
            onChange={setTab}
            type="line"
            className="toolbar-tabs"
          >
            {tabs.map(([name, count]) => (
              <TabPane key={name} title={`${wsLabel(name)} (${count})`} />
            ))}
          </Tabs>

          <div className="toolbar-right">
            <Input.Search
              allowClear
              placeholder="搜索标题或内容"
              value={search}
              onChange={setSearch}
              style={{ width: 230 }}
            />
            <Select
              value={filter}
              onChange={setFilter}
              options={FILTER_OPTIONS}
              style={{ width: 150 }}
            />
            <Button
              type="outline"
              disabled={!!progress.running || !modelConfigured || !compatible}
              onClick={() =>
                generate(
                  sessions
                    .filter((s) => s.old_source !== 'custom')
                    .map((s) => s.id),
                  false
                )
              }
            >
              生成缺失
            </Button>
            <Button
              disabled={
                !!progress.running ||
                selectedKeys.length === 0 ||
                !modelConfigured ||
                !compatible
              }
              onClick={() => generate(selectedKeys, true)}
            >
              重新生成选中
            </Button>
          </div>
        </div>

        {progress.running && (
          <div className="progress-line">
            <Progress
              percent={
                progress.total
                  ? Math.round((progress.done / progress.total) * 100)
                  : 0
              }
              showText={false}
              size="small"
            />
            <Text type="secondary" style={{ whiteSpace: 'nowrap' }}>
              生成中 {progress.done}/{progress.total}
            </Text>
          </div>
        )}
      </div>

      <div className="panel table-panel">
        <Table
          rowKey="id"
          columns={columns}
          data={filtered}
          loading={loading}
          pagination={false}
          rowSelection={{
            type: 'checkbox',
            selectedRowKeys: selectedKeys,
            onChange: (keys) => setSelectedKeys(keys),
          }}
          scroll={{ y: tableY }}
        />
      </div>

      <div className="apply-bar">
        <div className="apply-inner">
          <Text type="secondary">
            已选 <strong>{applicableCount}</strong> 条可应用，当前列表{' '}
            {filtered.length} 条
          </Text>
          <div className="apply-actions">
            <Button type="text" onClick={() => window.open('/api/export', '_blank')}>
              导出 JSON
            </Button>
            <Button onClick={savePlan}>保存方案</Button>
            <Button
              type="primary"
              disabled={!applicableCount || !compatible}
              loading={applying}
              onClick={openApply}
            >
              {applicableCount ? `应用 ${applicableCount} 条` : '应用选中标题'}
            </Button>
          </div>
        </div>
      </div>

      <Modal
        title="确认写回"
        visible={applyVisible}
        onCancel={() => setApplyVisible(false)}
        onOk={doApply}
        okText="确认写回"
        cancelText="取消"
        confirmLoading={applying}
      >
        <p className="modal-p">
          将把 <strong>{applicableCount}</strong> 个标题写回 ZCode。
        </p>
        <p className="modal-p">
          写之前会自动备份两个数据库。写回后这些标题会被标记为手动标题，ZCode
          以后不会自动改它们。
        </p>
      </Modal>

      <Modal
        title="请先退出 ZCode"
        visible={zcodeVisible}
        onCancel={() => setZcodeVisible(false)}
        onOk={doApply}
        okText="我已退出，重试"
        cancelText="稍后再说"
        confirmLoading={applying}
      >
        <p className="modal-p">
          请完全退出 ZCode：关掉主窗口，并右键托盘图标选择退出。
        </p>
        <p className="modal-p">
          侧栏读的是内存索引，运行中写库可能不刷新，甚至被旧数据覆盖。
        </p>
      </Modal>

      <Modal
        title={lastApply && lastApply.ok === false ? '写回有问题' : '写回完成'}
        visible={successVisible}
        onCancel={() => setSuccessVisible(false)}
        footer={
          <Space>
            <Button
              type="outline"
              status="danger"
              loading={reverting}
              onClick={revertLast}
            >
              撤销这次写回
            </Button>
            <Button onClick={() => setSuccessVisible(false)}>关闭</Button>
            <Button type="primary" onClick={openZcode}>
              打开 ZCode
            </Button>
          </Space>
        }
      >
        {lastApply && (
          <div>
            {lastApply.ok === false && (
              <p className="modal-p">
                {lastApply.message || '这次写回没有完全成功。'}
              </p>
            )}
            <p className="modal-p">
              已写回 <strong>{lastApply.applied}</strong> 个标题。
            </p>
            {lastApply.verify_errors?.length > 0 && (
              <p className="modal-p">
                校验不一致 {lastApply.verify_errors.length} 条，建议直接撤销这次写回。
              </p>
            )}
            {lastApply.skipped?.length > 0 && (
              <p className="modal-p">
                跳过 {lastApply.skipped.length} 条：
                {[...new Set(lastApply.skipped.map((s) => s.reason))].join('、')}
              </p>
            )}
            <p className="modal-p">
              备份：
              <Text code copyable className="backup-text">
                {lastApply.backup}
              </Text>
            </p>
            <p className="modal-p">
              如果结果不对，可以点「撤销这次写回」；撤销只恢复这次涉及的标题，不会覆盖之后的新会话。
            </p>
          </div>
        )}
      </Modal>

      <Modal
        title="设置"
        visible={settingsVisible}
        onCancel={() => setSettingsVisible(false)}
        footer={null}
        style={{ width: 640 }}
      >
        <div className="settings-row">
          <Text className="settings-label">模型 API 地址</Text>
          <Input
            value={settingsForm.api_url}
            onChange={(v) => updateSetting('api_url', v)}
            placeholder="https://api.example.com/v1/chat/completions"
          />
          <div className="settings-help">兼容 OpenAI Chat Completions 接口。</div>
        </div>

        <div className="settings-row">
          <Text className="settings-label">模型</Text>
          <Input
            value={settingsForm.model}
            onChange={(v) => updateSetting('model', v)}
            placeholder="deepseek-chat / gpt-4o-mini"
          />
        </div>

        <div className="settings-row">
          <Text className="settings-label">API Key</Text>
          <Input.Password
            value={settingsForm.api_key}
            onChange={(v) => updateSetting('api_key', v)}
            placeholder={
              settingsMeta.api_key_set
                ? '已保存，留空不修改'
                : '如果服务不需要 key，可以留空'
            }
          />
          <Checkbox
            checked={settingsForm.clear_api_key}
            onChange={(v) => updateSetting('clear_api_key', v)}
            style={{ marginTop: 8 }}
          >
            清除已保存的 API Key
          </Checkbox>
        </div>

        <div className="settings-row">
          <Text className="settings-label">ZCode 数据目录</Text>
          <Input
            value={settingsForm.zcode_dir}
            onChange={(v) => updateSetting('zcode_dir', v)}
            placeholder="C:\Users\你的用户名\.zcode"
          />
          <div className="settings-help">
            下面两个库会从该目录推导；修改后保存会重新扫描。
          </div>
          <div className="settings-help">tasks-index：{settingsMeta.tasks_db || '—'}</div>
          <div className="settings-help">session：{settingsMeta.sessions_db || '—'}</div>
          <div className="settings-help">工具数据目录：{settingsMeta.data_dir || '—'}</div>
        </div>

        {Object.values(settingsMeta.env_overrides || {}).some(Boolean) && (
          <Alert
            type="warning"
            showIcon
            content="部分字段由环境变量覆盖，保存不会改变它们的生效值。"
            style={{ marginBottom: 12 }}
          />
        )}

        {settingsError && (
          <Alert
            type="error"
            showIcon
            content={settingsError}
            style={{ marginBottom: 12 }}
          />
        )}

        <div className="settings-actions">
          <Button onClick={testConnection} loading={testingSettings}>
            测试连接
          </Button>
          <Button type="primary" onClick={saveSettings} loading={savingSettings}>
            保存
          </Button>
          <Button type="text" onClick={() => setSettingsVisible(false)}>
            取消
          </Button>
        </div>
      </Modal>
    </div>
  );
}
