/* PXE 装机管理平台 · 前端（Vue3 + Element Plus，无打包，直接由 FastAPI 托管） */
const { createApp, ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } = Vue;
const { ElMessage, ElMessageBox, ElLoading } = ElementPlus;

const TOKEN_KEY = 'pxe_token';
const state = reactive({
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: '',
  loggedIn: false,
  activeMenu: 'nodes',
});

/* ------------------------------------------------------------------ API */
async function api(path, options) {
  const opts = Object.assign({}, options || {});
  const headers = Object.assign({}, opts.headers || {});
  if (opts.json !== false) headers['Content-Type'] = 'application/json';
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  opts.headers = headers;

  const res = await fetch(path, opts);
  if (res.status === 401) {
    logout();
    throw new Error('登录已失效，请重新登录');
  }
  if (res.status === 404) {
    throw new Error('接口不存在（404）：' + path + '\n后端可能未升级：请确认已复制 app/ 与 static/ 目录，'
      + '并执行 systemctl restart pxe-web（系统设置页可查看当前版本）');
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = (d && (d.message || d.detail)) || (typeof d === 'string' ? d : '') || (data && data.message) || '请求失败';
    throw new Error(msg);
  }
  return data;
}

/* 带进度的上传（fetch 拿不到上传进度，ISO 太大必须用 XHR） */
function uploadWithProgress(url, fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    if (state.token) xhr.setRequestHeader('Authorization', 'Bearer ' + state.token);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status === 401) { logout(); reject(new Error('登录已失效，请重新登录')); return; }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(data); return; }
      const d = data && data.detail;
      reject(new Error(
        (d && (d.message || d.detail)) || (typeof d === 'string' ? d : '')
        || (data && data.message) || xhr.responseText || '上传失败'
      ));
    };
    xhr.onerror = () => reject(new Error('网络错误，上传中断'));
    xhr.send(fd);
  });
}

function logout() {
  state.token = '';
  state.loggedIn = false;
  state.user = '';
  localStorage.removeItem(TOKEN_KEY);
}

/* 登录后的欢迎语（欢快一点，顺带说明当前适配范围）
   排版交给 .welcome-* 那组样式，这里只管内容，别再手写内联样式 */
const WELCOME_HTML = `
<div class="welcome-hero">
  <span class="welcome-hero-icon">🎉</span>
  <span class="welcome-hero-text">欢迎使用 AI 算力集群快速部署和验证系统</span>
</div>
<div class="welcome-desc">装机、下发、验证一条龙搞定，剩下的时间用来喝咖啡 ☕</div>
<ul class="welcome-list">
  <li>
    <i>🖥️</i>
    <div><b>x86 平台</b>：主要适配 Ubuntu 系列，开箱即用</div>
  </li>
  <li>
    <i>💪</i>
    <div><b>ARM 平台</b>：主要适配 NVIDIA Grace CPU，为算力节点量身打造</div>
  </li>
  <li>
    <i>🧭</i>
    <div><b>其它架构与系统</b>：还在路上，后续版本会陆续安排上，敬请期待</div>
  </li>
</ul>
<div class="welcome-tip">💡 小贴士：遇到问题先看「运行日志」，大部分坑都藏在那里 🔍</div>
`;

function showWelcome() {
  ElMessageBox.alert(WELCOME_HTML, '欢迎回来', {
    dangerouslyUseHTMLString: true,
    confirmButtonText: '开始搞机',
    customClass: 'welcome-box',
    showClose: false,
  }).catch(() => {});
}

/* ------------------------------------------------------------------ 登录页 */
const LoginView = {
  setup() {
    const form = reactive({ username: '', password: '' });
    const loading = ref(false);
    // 登录页配图：static/img/ 下有图才显示，没图就整块不渲染，
    // 不留裂图、不留 alt 文字，卡片布局也不受影响
    const logo = ref('');

    onMounted(async () => {
      try {
        const res = await fetch('/api/branding', { cache: 'no-store' });
        if (!res.ok) return;
        const d = await res.json();
        const url = (d && d.logo) || '';
        if (!url) return;
        // 先悄悄预载，加载成功再挂上去，避免"先空后跳"的抖动
        await new Promise((resolve, reject) => {
          const probe = new Image();
          probe.onload = resolve;
          probe.onerror = reject;
          probe.src = url;
        });
        logo.value = url;
      } catch (e) { /* 没图或读不到就算了，不提示、不占位 */ }
    });

    async function submit() {
      if (!form.username || !form.password) {
        ElMessage.warning('请输入用户名和密码');
        return;
      }
      loading.value = true;
      try {
        const data = await api('/api/login', {
          method: 'POST',
          body: JSON.stringify({ username: form.username, password: form.password }),
        });
        state.token = data.token;
        state.user = data.user;
        state.loggedIn = true;
        localStorage.setItem(TOKEN_KEY, data.token);
        ElMessage.success('登录成功，欢迎回来 ' + data.user);
        showWelcome();
      } catch (e) {
        ElMessage.error(e.message || '登录失败');
      } finally {
        loading.value = false;
      }
    }

    return { form, loading, logo, submit };
  },
  template: `
    <div class="login-wrap">
      <div class="login-card">
        <img v-if="logo" class="login-mascot" :src="logo" alt="" />
        <div class="login-title">PXE 装机管理平台</div>
        <div class="login-sub">iPXE / DHCP / 装机节点统一管理</div>
        <el-form @submit.prevent="submit">
          <el-form-item>
            <el-input v-model="form.username" placeholder="用户名" size="large" autocomplete="username" />
          </el-form-item>
          <el-form-item>
            <el-input v-model="form.password" type="password" placeholder="密码" size="large"
                      show-password autocomplete="current-password" @keyup.enter="submit" />
          </el-form-item>
          <el-button type="primary" size="large" style="width:100%" :loading="loading" @click="submit">
            登 录
          </el-button>
        </el-form>
      </div>
    </div>
  `,
};

/* ------------------------------------------------------------------ 节点信息 */
const NodesView = {
  setup() {
    const nodes = ref([]);
    const loading = ref(false);
    const systems = ref([]);
    const systemsSource = ref('');
    const keyword = ref('');
    const autoRefresh = ref(false);
    const probeTtl = ref(0);
    let timer = null;

    const dialog = reactive({
      visible: false,
      isNew: true,
      form: { name: '', mac: '', ip: '', system: '' },
    });

    const rows = computed(() => {
      const kw = keyword.value.trim().toLowerCase();
      if (!kw) return nodes.value;
      return nodes.value.filter(n =>
        [n.name, n.ip, n.mac, n.system].join(' ').toLowerCase().includes(kw)
      );
    });

    // 架构决定系统列表来源：x86 → boot.ipxe 的 item；arm → grub.cfg 的 menuentry
    async function loadSystems() {
      try {
        const d = await api('/api/systems?arch=' + encodeURIComponent(dialog.form.arch));
        systems.value = d.items || [];
        systemsSource.value = d.source || '';
      } catch (e) { /* 忽略 */ }
    }

    // 一个菜单项都没有时，说清楚去哪儿造，而不是摆几个假选项
    const systemsHint = computed(() => {
      const isArm = dialog.form.arch === 'arm';
      const src = systemsSource.value || (isArm ? 'grub.cfg' : 'boot.ipxe');
      if (!systems.value.length) {
        return '还没读到菜单项，请先到「镜像管理」添加镜像（写入 ' + src + ' 后这里才会出现）。'
          + '若想始终显示固定清单，可给服务配置 PXE_DEFAULT_SYSTEMS 环境变量。';
      }
      return (isArm ? '来源：grub.cfg 的 menuentry（' : '来源：boot.ipxe 的 item（') + src + '）';
    });

    function onArchChange(val) {
      dialog.form.system = '';
      loadSystems();
    }

    // force=true 强制重新探测（要等满超时，慢）；默认走后端缓存，列表秒开
    async function load(force) {
      loading.value = true;
      try {
        const d = await api('/api/nodes' + (force ? '?probe=force' : ''));
        nodes.value = d.items || [];
        probeTtl.value = d.probe_ttl || 0;
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        loading.value = false;
      }
    }

    function statusType(status) {
      if (status === '在线') return 'success';
      if (status === '可达(SSH未开)') return 'warning';
      if (status === '离线') return 'info';
      return 'danger';
    }

    function openCreate() {
      dialog.isNew = true;
      dialog.form = { name: '', mac: '', ip: '', system: '', arch: 'x86' };
      loadSystems().then(() => { dialog.form.system = systems.value[0] || ''; });
      dialog.visible = true;
    }

    function openEdit(row) {
      dialog.isNew = false;
      dialog.form = { name: row.name, mac: row.mac, ip: row.ip, system: row.system, arch: row.arch || 'x86' };
      loadSystems();
      dialog.visible = true;
    }

    function escapeHtml(s) {
      return String(s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }

    // 保存/删除后：成功只提示一句话，重载失败则把 systemctl 原文弹出来
    function notifyResult(d) {
      if (d && d.reload_ok === false) {
        ElMessageBox.alert(
          '<pre style="white-space:pre-wrap;text-align:left;margin:0;font-size:12px">'
          + escapeHtml(d.reload || '无输出') + '</pre>',
          '已写入配置，但 dhcpd 重载失败',
          { dangerouslyUseHTMLString: true, confirmButtonText: '知道了', type: 'warning' }
        );
      } else {
        ElMessage.success((d && d.message) || '操作成功');
      }
    }

    async function save() {
      if (!dialog.form.name) { ElMessage.warning('节点名不能为空'); return; }
      try {
        const d = await api('/api/nodes', { method: 'POST', body: JSON.stringify(dialog.form) });
        dialog.visible = false;
        notifyResult(d);
        await load(true);
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    async function remove(row) {
      try {
        await ElMessageBox.confirm('确认删除节点「' + row.name + '」？该操作会写回 dhcpd 配置。', '删除确认', {
          type: 'warning',
          confirmButtonText: '删除',
          cancelButtonText: '取消',
        });
      } catch (e) { return; }
      try {
        const d = await api('/api/nodes/' + encodeURIComponent(row.name), { method: 'DELETE' });
        notifyResult(d);
        await load(true);
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    function toggleAuto(val) {
      if (timer) { clearInterval(timer); timer = null; }
      if (val) timer = setInterval(load, 15000);
    }

    onMounted(async () => {
      await loadSystems();
      await load(true);
    });

    onBeforeUnmount(() => { if (timer) clearInterval(timer); });

    return {
      nodes, rows, loading, systems, systemsSource, systemsHint, keyword, autoRefresh, probeTtl, dialog,
      load, save, remove, openCreate, openEdit, statusType, toggleAuto, onArchChange,
      openTerminal: (row) => window.__openTerminal(row),
    };
  },
  template: `
    <div class="panel">
      <div class="panel-title">节点信息</div>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">新增节点</el-button>
        <el-button @click="load(true)" :loading="loading">重新探测</el-button>
        <el-input v-model="keyword" placeholder="搜索 名称 / IP / MAC / 系统" clearable style="width:260px" />
        <el-checkbox v-model="autoRefresh" @change="toggleAuto">15 秒自动刷新</el-checkbox>
        <span style="color:#909399;font-size:12px">共 {{ rows.length }} 个节点</span>
        <span v-if="probeTtl" style="color:#909399;font-size:12px">
          在线状态缓存 {{ probeTtl }} 秒，「重新探测」可强制刷新
        </span>
      </div>

      <el-table :data="rows" v-loading="loading" border stripe size="default">
        <el-table-column prop="name" label="节点名" min-width="120" />
        <el-table-column prop="ip" label="节点 IP" min-width="130">
          <template #default="{ row }">
            <span class="text-mono">{{ row.ip || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="system" label="节点系统" min-width="150">
          <template #default="{ row }">
            <el-tag v-if="row.system" type="primary" effect="plain">{{ row.system }}</el-tag>
            <span v-else style="color:#c0c4cc">未指定</span>
          </template>
        </el-table-column>
        <el-table-column label="架构" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.arch === 'arm' ? 'warning' : 'primary'" effect="dark">
              {{ row.arch || 'x86' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="mac" label="MAC" min-width="170">
          <template #default="{ row }">
            <span class="text-mono">{{ row.mac || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="150">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" effect="dark" size="small">{{ row.status }}</el-tag>
            <span style="color:#c0c4cc;font-size:11px;margin-left:6px">{{ row.checked_at }}</span>
          </template>
        </el-table-column>
        <el-table-column label="SSH 功能" width="100" fixed="right">
          <template #default="{ row }">
            <el-button type="success" size="small" :disabled="!row.ip" @click="openTerminal(row)">SSH</el-button>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <span style="color:#909399">暂无节点，点击「新增节点」创建，或检查 PXE_HOSTS_DIR 配置</span>
        </template>
      </el-table>

      <el-dialog v-model="dialog.visible" :title="dialog.isNew ? '新增节点' : '编辑节点'" width="460px">
        <el-form label-width="90px">
          <el-form-item label="节点名">
            <el-input v-model="dialog.form.name" :disabled="!dialog.isNew" placeholder="如 server-01" />
          </el-form-item>
          <el-form-item label="MAC 地址">
            <el-input v-model="dialog.form.mac" placeholder="BC:24:11:8E:C1:E4" class="text-mono" />
          </el-form-item>
          <el-form-item label="节点 IP">
            <el-input v-model="dialog.form.ip" placeholder="192.168.1.150" class="text-mono" />
          </el-form-item>
          <el-form-item label="架构" required>
            <el-radio-group v-model="dialog.form.arch" @change="onArchChange">
              <el-radio-button label="x86" />
              <el-radio-button label="arm" />
            </el-radio-group>
          </el-form-item>
          <el-form-item label="安装系统">
            <el-select v-model="dialog.form.system" style="width:100%"
                       :placeholder="systems.length ? '选择安装系统' : '暂无可用菜单项'">
              <el-option v-for="s in systems" :key="s" :label="s" :value="s" />
            </el-select>
            <div style="color:#909399;font-size:12px;line-height:1.4">{{ systemsHint }}</div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialog.visible = false">取消</el-button>
          <el-button type="primary" @click="save">保存并重载</el-button>
        </template>
      </el-dialog>
    </div>
  `,
};

/* ------------------------------------------------------------------ 节点发现 */
const DiscoveryView = {
  setup() {
    const items = ref([]);
    const leasesPath = ref('');
    const loading = ref(false);
    const auto = ref(false);
    const showAll = ref(false);
    const pxeOnly = ref(true);   // 默认只看拿到过引导文件（走了 PXE 启动）的机器
    const pending = ref(0);
    const stats = reactive({
      leases_total: 0, pxe: 0, pending_pxe: 0,
      pxe_syslog: 0, pxe_leases: 0, tftp_events: 0, grace_min: 10,
    });
    const syslog = ref('');
    const syslogExists = ref(false);
    const leasesTz = ref('utc');
    const scan = reactive({ total: 0, expired: 0, inactive: 0, kept: 0 });
    let timer = null;

    const dialog = reactive({
      visible: false,
      form: { name: '', mac: '', ip: '', arch: 'x86', system: '' },
    });
    const systems = ref([]);
    const submitting = ref(false);

    // fresh=true 跳过后端 syslog 解析缓存重新扫
    async function load(fresh) {
      loading.value = true;
      try {
        const d = await api('/api/discovery?pxe_only=' + (pxeOnly.value ? 1 : 0)
          + (fresh ? '&fresh=1' : ''));
        items.value = d.items || [];
        leasesPath.value = d.leases || '';
        pending.value = d.pending || 0;
        stats.leases_total = d.leases_total || 0;
        stats.pxe = d.pxe || 0;
        stats.pending_pxe = d.pending_pxe || 0;
        stats.pxe_syslog = d.pxe_syslog || 0;
        stats.pxe_leases = d.pxe_leases || 0;
        stats.tftp_events = d.tftp_events || 0;
        stats.grace_min = d.grace_min || 10;
        syslog.value = d.syslog || '';
        syslogExists.value = !!d.syslog_exists;
        leasesTz.value = d.leases_tz || 'utc';
        const s = d.scan || {};
        scan.total = s.total || 0;
        scan.expired = s.expired || 0;
        scan.inactive = s.inactive || 0;
        scan.kept = s.kept || 0;
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        loading.value = false;
      }
    }

    const rows = computed(() =>
      showAll.value ? items.value : items.value.filter(i => i.pending)
    );

    async function loadSystems() {
      try {
        const d = await api('/api/systems?arch=' + encodeURIComponent(dialog.form.arch));
        systems.value = d.items || [];
      } catch (e) { /* 忽略 */ }
    }

    const systemsHint = computed(() => {
      if (systems.value.length) return '';
      return '还没读到菜单项，请先到「镜像管理」添加镜像（'
        + (dialog.form.arch === 'arm' ? 'grub.cfg' : 'boot.ipxe') + '）。';
    });

    function onArchChange() {
      dialog.form.system = '';
      loadSystems();
    }

    function openRegister(row) {
      dialog.form = {
        name: row.hostname || ('host-' + String(row.ip).split('.').pop()),
        mac: row.mac_upper || '',
        ip: row.ip,
        arch: row.arch || 'x86',
        system: '',
      };
      loadSystems();
      dialog.visible = true;
    }

    async function submit() {
      if (!dialog.form.name) { ElMessage.warning('请填写节点名'); return; }
      submitting.value = true;
      try {
        const d = await api('/api/nodes', { method: 'POST', body: JSON.stringify(dialog.form) });
        if (d.reload_ok === false) {
          ElMessageBox.alert(String(d.reload || '无输出'), '已保存，但 dhcpd 重载失败',
            { type: 'warning', confirmButtonText: '知道了' });
        } else {
          ElMessage.success(d.message || '已登记');
        }
        dialog.visible = false;
        await load();
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        submitting.value = false;
      }
    }

    async function release(row) {
      try {
        await ElMessageBox.confirm(
          '确认释放 ' + row.ip + (row.mac_upper ? '（' + row.mac_upper + '）' : '')
          + ' 的 DHCP 租约？\n释放后该地址会回到地址池，机器下次获取地址时会重新分配。',
          '释放租约',
          // msg-pre：让文案里的 \n 真的换行，否则 HTML 会把空白折叠成一行
          { type: 'warning', confirmButtonText: '释放', cancelButtonText: '取消',
            customClass: 'msg-pre' }
        );
      } catch (e) { return; }
      try {
        const d = await api('/api/discovery/lease?ip=' + encodeURIComponent(row.ip)
          + '&mac=' + encodeURIComponent(row.mac || ''), { method: 'DELETE' });
        ElMessage.success(d.message || '已释放');
        await load();
      } catch (e) {
        ElMessageBox.alert(
          '<pre style="white-space:pre-wrap;text-align:left;margin:0;font-size:12px">'
          + String(e.message).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
          + '</pre>',
          '释放失败',
          { dangerouslyUseHTMLString: true, confirmButtonText: '知道了', type: 'error' }
        );
      }
    }

    function statusType(status) {
      if (status === '未登记') return 'danger';
      if (status === '已登记未选系统') return 'warning';
      return 'success';
    }

    function toggleAuto(val) {
      if (timer) { clearInterval(timer); timer = null; }
      if (val) timer = setInterval(load, 15000);
    }

    onMounted(load);
    onBeforeUnmount(() => { if (timer) clearInterval(timer); });

    return {
      items, rows, leasesPath, loading, auto, showAll, pxeOnly, pending, stats,
      syslog, syslogExists, leasesTz, scan, dialog, systems, systemsHint, submitting,
      load, openRegister, submit, release, statusType, toggleAuto, onArchChange,
    };
  },
  template: `
    <div class="panel">
      <div class="panel-title">节点发现</div>
      <div class="toolbar">
        <el-button @click="load(true)" :loading="loading">刷新</el-button>
        <el-checkbox v-model="pxeOnly" @change="load">只看拿到过引导文件的（PXE 启动）</el-checkbox>
        <el-checkbox v-model="showAll">显示全部（含已选系统的）</el-checkbox>
        <el-checkbox v-model="auto" @change="toggleAuto">15 秒自动刷新</el-checkbox>
        <el-tag v-if="pending" type="danger" effect="dark">{{ pending }} 台待处理</el-tag>
        <span style="color:#909399;font-size:12px">租约文件：{{ leasesPath }}</span>
      </div>

      <div style="color:#909399;font-size:12px;margin-bottom:10px">
        默认只列<b>真正走了 PXE 启动</b>、但<b>还没有登记安装系统</b>的主机
        （这些机器会停在 grub 的 “Boot from next volume” 或 iPXE 的 “shell” 上）。
      </div>
      <div style="color:#909399;font-size:12px;margin-bottom:10px">
        判定方式：isc-dhcp 的 <code>dhcpd.leases</code> <b>不记录 filename</b>，所以按
        <b>「IP + 时间落在租约有效期内（两端各放宽 {{ stats.grace_min }} 分钟）」</b>
        去 <code>{{ syslog || 'syslog' }}</code> 里找 tftpd 的传输记录
        （形如 <code>in.tftpd[1234]: RRQ from 192.168.1.50 filename grubnetx64.efi</code>）；
        租约里若本来就写了 filename 则直接采用。
        当前租约 {{ stats.leases_total }} 条，tftpd 记录 {{ stats.tftp_events }} 条，
        判定为 PXE 的 {{ stats.pxe }} 条（syslog {{ stats.pxe_syslog }} ／ 租约 {{ stats.pxe_leases }}）。
        <span v-if="!syslogExists" style="color:#e6a23c">
          ⚠ 读不到 {{ syslog || 'syslog' }}，只能通过租约里的 filename 判断，可用 PXE_SYSLOG_FILE 指定路径。
        </span>
        <span v-else-if="pxeOnly && stats.pxe === 0" style="color:#e6a23c">
          ⚠ 一条都没匹配上：确认 tftpd 是否把日志写进了该文件（rsyslog 是否开启），
          或适当调大 PXE_TFTP_GRACE_MIN；也可先取消上面的勾选看全部租约。
        </span>
      </div>

      <div style="color:#909399;font-size:12px;margin-bottom:10px">
        租约文件共 {{ scan.total }} 条记录：判为过期 {{ scan.expired }} 条、
        非 active {{ scan.inactive }} 条，实际采用 <b>{{ scan.kept }}</b> 条。
        租约时间按 <b>{{ leasesTz === 'local' ? '本地时间' : 'UTC' }}</b> 解析
        <span v-if="leasesTz === 'utc'">（isc-dhcp 默认就写 UTC，跟系统时区无关）</span>。
        <span v-if="scan.total && !scan.kept" style="color:#e6a23c">
          ⚠ 全被过滤掉了：若服务器不在 UTC 时区且 dhcpd.conf 里配了
          <code>db-time-format local;</code>，可用 <code>PXE_LEASES_TZ=local</code> 纠正。
        </span>
      </div>

      <el-table :data="rows" v-loading="loading" border stripe>
        <el-table-column prop="ip" label="已分配 IP" min-width="130">
          <template #default="{ row }"><span class="text-mono">{{ row.ip }}</span></template>
        </el-table-column>
        <el-table-column prop="mac_upper" label="MAC" min-width="170">
          <template #default="{ row }"><span class="text-mono">{{ row.mac_upper || '-' }}</span></template>
        </el-table-column>
        <el-table-column prop="hostname" label="主机名" min-width="140">
          <template #default="{ row }">{{ row.hostname || '-' }}</template>
        </el-table-column>
        <el-table-column prop="filename" label="引导文件" min-width="200">
          <template #default="{ row }">
            <el-tag v-if="row.filename" size="small" type="success" effect="plain" class="text-mono">
              {{ row.filename }}
            </el-tag>
            <span v-else-if="row.pxe" style="color:#e6a23c">已连 tftpd，未取到文件</span>
            <span v-else style="color:#c0c4cc">未获取（只是要了个 IP）</span>
            <div v-if="row.pxe" style="color:#909399;font-size:11px;margin-top:2px">
              来源：{{ row.boot_source === 'syslog' ? 'tftpd 日志' : 'dhcpd 租约' }}
              <span v-if="row.boot_time"> · {{ row.boot_time }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="node" label="已登记节点" min-width="140">
          <template #default="{ row }">{{ row.node || '-' }}</template>
        </el-table-column>
        <el-table-column prop="system" label="已选系统" min-width="150">
          <template #default="{ row }">
            <el-tag v-if="row.system" size="small" type="primary" effect="plain">{{ row.system }}</el-tag>
            <span v-else style="color:#c0c4cc">未选择</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="160">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" effect="dark" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="ends" label="租约到期" width="120" />
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" @click="openRegister(row)">登记</el-button>
            <el-button size="small" type="danger" @click="release(row)">释放租约</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <span style="color:#909399">
            没有匹配的主机（读不到租约文件请检查 PXE_LEASES_FILE；
            若确认机器已经 PXE 启动过却仍为空，可取消「只看拿到过引导文件的」勾选）
          </span>
        </template>
      </el-table>

      <el-dialog v-model="dialog.visible" title="登记为节点" width="460px">
        <el-form label-width="90px">
          <el-form-item label="节点名">
            <el-input v-model="dialog.form.name" />
          </el-form-item>
          <el-form-item label="MAC 地址">
            <el-input v-model="dialog.form.mac" class="text-mono" />
          </el-form-item>
          <el-form-item label="节点 IP">
            <el-input v-model="dialog.form.ip" class="text-mono" />
          </el-form-item>
          <el-form-item label="架构" required>
            <el-radio-group v-model="dialog.form.arch" @change="onArchChange">
              <el-radio-button label="x86" />
              <el-radio-button label="arm" />
            </el-radio-group>
          </el-form-item>
          <el-form-item label="安装系统">
            <el-select v-model="dialog.form.system" style="width:100%"
                       :placeholder="systems.length ? '选择安装系统' : '暂无可用菜单项'">
              <el-option v-for="s in systems" :key="s" :label="s" :value="s" />
            </el-select>
            <div v-if="systemsHint" style="color:#909399;font-size:12px;line-height:1.4">
              {{ systemsHint }}
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="submitting" @click="submit">保存并重载</el-button>
        </template>
      </el-dialog>
    </div>
  `,
};

/* ------------------------------------------------------------------ 镜像管理 */
const ImagesView = {
  setup() {
    const images = ref([]);
    const dirPath = ref('');
    const loading = ref(false);
    const submitting = ref(false);
    const progress = ref(0);
    const savingMenu = ref(false);
    const menuDialog = reactive({
      visible: false, name: '', arch: '', source: '', header: '', footer: '',
      content: '', exists: true, mismatch: false, expected: '',
    });
    const dialog = reactive({
      visible: false,
      name: '',
      arch: 'x86',
      ukiType: '新系统类型',   // 选中 UKI 里已有的系统类型时，只传 ISO + 可选 meta/user
      iso: null, initrd: null, vmlinuz: null, meta: null, user: null,
    });
    // 变更这个值会强制重建下面的 el-upload，彻底清空上次已选中的文件列表
    const uploadKey = ref(0);
    const ukiTypes = ref([]);
    const ukiDir = ref('');
    const NEW_TYPE = '新系统类型';

    async function load() {
      loading.value = true;
      try {
        const d = await api('/api/images');
        images.value = d.items || [];
        dirPath.value = d.dir || '';
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        loading.value = false;
      }
    }

    function pick(key, file) { dialog[key] = file && file.raw ? file.raw : null; }
    function clear(key) { dialog[key] = null; }

    // 「新系统类型」= 自己起名、自己传内核；选中已有类型则只传 ISO
    const isNewType = computed(() => !dialog.ukiType || dialog.ukiType === NEW_TYPE);

    async function loadUkiTypes() {
      try {
        const d = await api('/api/uki-types?arch=' + encodeURIComponent(dialog.arch));
        ukiTypes.value = d.items || [];
        ukiDir.value = d.dir || '';
      } catch (e) { ukiTypes.value = []; }
      // "新系统类型"永远排在最末尾：以后往 UKI 里加目录不会插到中间
      dialog.ukiType = NEW_TYPE;
    }

    function resetForm() {
      dialog.name = '';
      dialog.arch = 'x86';
      dialog.ukiType = NEW_TYPE;
      dialog.iso = dialog.initrd = dialog.vmlinuz = dialog.meta = dialog.user = null;
      uploadKey.value += 1;   // 重置 el-upload 内部文件列表
      loadUkiTypes();
    }

    function openCreate() {
      resetForm();
      dialog.visible = true;
    }

    const canSubmit = computed(() => {
      if (!dialog.arch || !dialog.iso) return false;
      if (isNewType.value) {
        return !!dialog.name && !!dialog.initrd && !!dialog.vmlinuz;
      }
      return true;   // 已有系统类型：镜像名取类型名，只传 ISO
    });

    async function submit() {
      if (!canSubmit.value) {
        ElMessage.warning(isNewType.value
          ? '镜像名称、架构、ISO、initrd、vmlinuz 为必填项'
          : '请先选择 ISO 文件');
        return;
      }
      const fd = new FormData();
      fd.append('name', isNewType.value ? dialog.name : dialog.ukiType);
      fd.append('arch', dialog.arch);
      fd.append('iso', dialog.iso);
      // 已有系统类型：内核 / initrd 由 UKI 目录自带，不允许上传；
      // 但 meta-data / user-data 始终可以选传（平台不强制改名）。
      // 新系统类型：vmlinuz / initrd 必传，meta / user 可选。
      if (!isNewType.value) {
        fd.append('uki_type', dialog.ukiType);
      } else {
        fd.append('initrd', dialog.initrd);
        fd.append('vmlinuz', dialog.vmlinuz);
      }
      if (dialog.meta) fd.append('meta_data', dialog.meta);
      if (dialog.user) fd.append('user_data', dialog.user);

      submitting.value = true;
      progress.value = 0;
      try {
        const d = await uploadWithProgress('/api/images', fd, p => { progress.value = p; });
        ElMessage.success(d && d.message ? d.message : '添加成功');
        dialog.visible = false;
        resetForm();          // 上传完成后清干净，下次打开是空白表单
        await load();
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        submitting.value = false;
      }
    }

    function fillMenuDialog(d, exists) {
      menuDialog.name = d.name;
      menuDialog.arch = d.arch;
      menuDialog.source = d.source;
      menuDialog.header = d.header;
      menuDialog.footer = d.footer;
      menuDialog.content = d.content;
      menuDialog.exists = !!exists;
      menuDialog.mismatch = !!d.mismatch;
      menuDialog.expected = d.expected || d.source;
      menuDialog.visible = true;
    }

    async function openMenu(row) {
      const url = '/api/images/' + encodeURIComponent(row.name) + '/menu';
      try {
        const d = await api(url);
        fillMenuDialog(d, d.exists !== false);
      } catch (e) {
        const msg = String((e && e.message) || e);
        const tip = msg.indexOf('404') >= 0
          ? '\n\n多半是后端还没升级：\n  cp -rf pxe-web/* /opt/pxe-web/ && systemctl restart pxe-web\n再到「系统设置」确认平台版本。'
          : '';
        const html = '<pre style="white-space:pre-wrap;text-align:left;margin:0;font-size:12px">'
          + msg.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
          + tip + '</pre>';
        // 菜单项缺失：把异常说清楚，同时留一个「用默认模板创建」的出口
        try {
          await ElMessageBox.confirm(
            html,
            '无法打开启动配置编辑器',
            {
              dangerouslyUseHTMLString: true, type: 'error',
              confirmButtonText: '用默认模板创建', cancelButtonText: '关闭',
              distinguishCancelAndClose: true,
            }
          );
          const d = await api(url + '?allow_create=1');
          fillMenuDialog(d, false);
        } catch (inner) {
          if (inner !== 'cancel' && inner !== 'close') ElMessage.error(String(inner && inner.message || inner));
        }
      }
    }

    async function saveMenu() {
      savingMenu.value = true;
      try {
        const d = await api('/api/images/' + encodeURIComponent(menuDialog.name) + '/menu', {
          method: 'PUT',
          body: JSON.stringify({ content: menuDialog.content }),
        });
        ElMessage.success(d.message || '已更新');
        menuDialog.visible = false;
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        savingMenu.value = false;
      }
    }

    async function prewarm(row) {
      try {
        const d = await api('/api/images/' + encodeURIComponent(row.name)
          + '/prewarm?deep=1', { method: 'POST', body: '{}' });
        ElMessage.success(d.message || '已预热');
      } catch (e) { ElMessage.error(e.message); }
    }

    async function remove(row) {
      try {
        await ElMessageBox.confirm(
          '确认删除镜像「' + row.name + '」（' + (row.arch || 'x86') + '）？\n\n目录：'
          + dirPath.value + (row.uki
            ? '/UKI/' + (row.arch || 'x86') + '/' + row.name
            : '/' + (row.arch || 'x86') + '/' + row.name)
          + '\n\n' + (row.uki
            ? 'UKI 类型只清掉 ISO / autoinstall 与标记文件，内置的 vmlinuz / initrd 会保留。'
            : '将删除该目录下的全部文件，并同步移除菜单项。'),
          '删除确认',
          // msg-pre：\n 要真的换行，否则整段挤成一行；路径用等宽字体好认
          { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消',
            customClass: 'msg-pre' }
        );
      } catch (e) { return; }
      try {
        const d = await api('/api/images/' + encodeURIComponent(row.name), { method: 'DELETE' });
        ElMessage.success(d.message || '已删除');
        await load();
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    onMounted(load);

    return {
      images, dirPath, loading, submitting, progress, dialog, menuDialog, savingMenu,
      uploadKey, ukiTypes, ukiDir, isNewType, NEW_TYPE,
      load, submit, remove, prewarm, openCreate, openMenu, saveMenu,
      loadUkiTypes, pick, clear, canSubmit,
    };
  },
  template: `
    <div class="panel">
      <div class="panel-title">镜像管理</div>
      <div class="toolbar">
        <el-button type="primary" @click="openCreate">添加镜像</el-button>
        <el-button @click="load" :loading="loading">刷新</el-button>
        <span style="color:#909399;font-size:12px">镜像目录：{{ dirPath }}</span>
      </div>

      <el-table :data="images" v-loading="loading" border stripe>
        <el-table-column prop="name" label="镜像名称" min-width="140" />
        <el-table-column label="架构" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.arch === 'arm' ? 'warning' : 'primary'" effect="dark">
              {{ row.arch || 'x86' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="iso" label="ISO" min-width="200" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-mono">{{ row.iso || '-' }}</span></template>
        </el-table-column>
        <el-table-column label="内核" min-width="210" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="text-mono">{{ row.vmlinuz || '—' }}</div>
            <div class="text-mono" style="color:#909399">{{ row.initrd || '—' }}</div>
            <el-tag v-if="row.uki" size="small" type="info" effect="plain" style="margin-top:2px">
              UKI 内置
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="自动安装" min-width="180">
          <template #default="{ row }">
            <el-tag v-for="f in row.autoinstall" :key="f" size="small" type="warning"
                    effect="plain" style="margin-right:4px">{{ f }}</el-tag>
            <span v-if="!row.autoinstall.length" style="color:#c0c4cc">
              {{ row.autoinstall_dir ? 'autoinstall/ 已创建（空）' : '无' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="size_human" label="大小" width="100" />
        <el-table-column prop="mtime" label="更新时间" width="150" />
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openMenu(row)">编辑</el-button>
            <el-button size="small" type="success" @click="prewarm(row)">预热</el-button>
            <el-button size="small" type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <span style="color:#909399">暂无镜像，点击「添加镜像」上传 ISO / initrd / vmlinuz</span>
        </template>
      </el-table>

      <el-dialog v-model="dialog.visible" title="添加镜像" width="580px">
        <el-form label-width="110px">
          <el-form-item label="架构" required>
            <el-radio-group v-model="dialog.arch" @change="loadUkiTypes">
              <el-radio-button label="x86" />
              <el-radio-button label="arm" />
            </el-radio-group>
          </el-form-item>
          <el-form-item label="系统类型" required>
            <el-select v-model="dialog.ukiType" style="width:100%">
              <el-option v-for="t in ukiTypes" :key="t" :label="t" :value="t" />
              <!-- 「新系统类型」固定放最末尾，以后往 UKI 里加目录不会插到中间 -->
              <el-option :label="NEW_TYPE" :value="NEW_TYPE" />
            </el-select>
            <div style="color:#909399;font-size:12px;line-height:1.5">
              来源：{{ ukiDir || '（读不到 UKI 目录）' }}<br />
              <template v-if="isNewType">
                已选「{{ NEW_TYPE }}」：自己起镜像名，ISO / vmlinuz / initrd 都要上传，
                会自动创建 {{ dirPath }}/{{ dialog.arch }}/&lt;镜像名&gt;/ 与 autoinstall/。
              </template>
              <template v-else>
                已选「{{ dialog.ukiType }}」：vmlinuz / initrd 用该目录里内置的那份，
                <b>只需要上传 ISO</b>（meta-data / user-data 可选），会放进
                {{ dirPath }}/UKI/{{ dialog.arch }}/{{ dialog.ukiType }}/。
              </template>
            </div>
          </el-form-item>
          <el-form-item v-if="isNewType" label="镜像名称" required>
            <el-input v-model="dialog.name"
                      placeholder="如 ubuntu-desk-24（作为目录名和 iPXE 菜单项名）" />
          </el-form-item>
          <el-form-item label="ISO 文件" required>
            <el-upload :key="uploadKey" action="/api/images" :auto-upload="false" :limit="1"
                       :on-change="(f) => pick('iso', f)" :on-remove="() => clear('iso')">
              <el-button size="small" type="primary">选择 ISO</el-button>
            </el-upload>
          </el-form-item>
          <template v-if="isNewType">
            <el-form-item label="vmlinuz" required>
              <el-upload :key="uploadKey" action="/api/images" :auto-upload="false" :limit="1"
                         :on-change="(f) => pick('vmlinuz', f)" :on-remove="() => clear('vmlinuz')">
                <el-button size="small" type="primary">选择 vmlinuz</el-button>
              </el-upload>
            </el-form-item>
            <el-form-item label="initrd" required>
              <el-upload :key="uploadKey" action="/api/images" :auto-upload="false" :limit="1"
                         :on-change="(f) => pick('initrd', f)" :on-remove="() => clear('initrd')">
                <el-button size="small" type="primary">选择 initrd</el-button>
              </el-upload>
            </el-form-item>
          </template>
          <!-- meta-data / user-data 不管新旧系统类型都可以选传 -->
          <el-form-item label="meta-data">
            <el-upload :key="uploadKey" action="/api/images" :auto-upload="false" :limit="1"
                       :on-change="(f) => pick('meta', f)" :on-remove="() => clear('meta')">
              <el-button size="small">选择 meta-data（可选）</el-button>
            </el-upload>
          </el-form-item>
          <el-form-item label="user-data">
            <el-upload :key="uploadKey" action="/api/images" :auto-upload="false" :limit="1"
                       :on-change="(f) => pick('user', f)" :on-remove="() => clear('user')">
              <el-button size="small">选择 user-data（可选）</el-button>
            </el-upload>
          </el-form-item>
        </el-form>
        <div class="upload-note">
          <div>上传的文件<b>一律保持原文件名</b>，平台不会重命名；并在
            {{ dialog.arch === 'x86' ? 'boot.ipxe' : 'grub.cfg' }} 中追加菜单项与安装标签。</div>
          <div v-if="dialog.meta || dialog.user">
            另外会在 autoinstall/ 下复制一份 <b>cloud-init 标准名副本</b>
            （meta-data / user-data<template v-if="dialog.arch === 'arm'">，以及 DGX 用的 server.yaml</template>），
            否则自动装机读不到。
          </div>
        </div>
        <el-progress v-if="submitting" :percentage="progress" style="margin-bottom:10px" />
        <template #footer>
          <el-button @click="dialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
            {{ submitting ? '上传中 ' + progress + '%' : '开始上传' }}
          </el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="menuDialog.visible"
                 :title="'编辑启动配置 · ' + menuDialog.name + '（' + menuDialog.arch + '）'" width="860px">
        <div style="color:#909399;font-size:12px;margin-bottom:8px">
          文件：{{ menuDialog.source }}
        </div>
        <el-alert v-if="!menuDialog.exists" type="warning" :closable="false" show-icon
                  title="菜单文件里还没有这个镜像的启动配置，下面是按默认模板生成的内容，确认后会自动创建。" />
        <el-alert v-if="menuDialog.exists && menuDialog.mismatch" type="warning" :closable="false" show-icon
                  :title="'只在 ' + menuDialog.source + ' 里找到了配置，按当前架构本应写在 '
                        + menuDialog.expected + '。请确认镜像架构是否正确。'" />
        <div class="menu-head text-mono">{{ menuDialog.header }}</div>
        <el-input v-model="menuDialog.content" type="textarea" :rows="14"
                  spellcheck="false" class="text-mono menu-editor" />
        <div v-if="menuDialog.footer" class="menu-head text-mono">{{ menuDialog.footer }}</div>
        <div style="color:#909399;font-size:12px;margin-top:8px">
          标题行与首尾花括号不可修改，只能编辑中间内容；点击「取消」不会写入任何改动。
          <span v-if="!menuDialog.exists">保存时会自动补上菜单项（x86 还会补 item 行）。</span>
        </div>
        <template #footer>
          <el-button @click="menuDialog.visible = false">取消</el-button>
          <el-button type="primary" :loading="savingMenu" @click="saveMenu">
            {{ menuDialog.exists ? '确认' : '创建并保存' }}
          </el-button>
        </template>
      </el-dialog>
    </div>
  `,
};

/* ------------------------------------------------------------------ 部署任务 */
const TasksView = {
  setup() {
    const scripts = ref([]);
    const nodes = ref([]);
    const dirPath = ref('');
    const script = ref('');
    const selNodes = ref([]);
    const user = ref('root');
    const password = ref('');
    const running = ref(false);
    const results = ref([]);
    const uploadVisible = ref(false);
    const file = ref(null);
    const concurrency = ref(5);      // 同时下发几台，避免几十台一起拉 ISO
    const batchDelay = ref(0);       // 每批之间额外等待秒数
    const logsVisible = ref(false);
    const logGroups = ref([]);
    const logDir = ref('');
    const curSn = ref('');
    const logText = ref('');
    const loadingLog = ref(false);

    async function loadScripts() {
      try {
        const d = await api('/api/scripts');
        scripts.value = d.items || [];
        dirPath.value = d.dir || '';
      } catch (e) { ElMessage.error(e.message); }
    }

    async function loadNodes() {
      try {
        const d = await api('/api/nodes');
        nodes.value = d.items || [];
      } catch (e) { ElMessage.error(e.message); }
    }

    async function loadAll() { await Promise.all([loadScripts(), loadNodes()]); }

    function onPick(f) { file.value = f && f.raw ? f.raw : null; }

    async function uploadScript() {
      if (!file.value) { ElMessage.warning('请先选择脚本文件'); return; }
      const fd = new FormData();
      fd.append('file', file.value);
      try {
        const d = await api('/api/scripts', { method: 'POST', body: fd, json: false });
        ElMessage.success(d.message || '已上传');
        uploadVisible.value = false;
        file.value = null;
        await loadScripts();
      } catch (e) { ElMessage.error(e.message); }
    }

    async function removeScript(row) {
      try {
        await ElMessageBox.confirm(
          '确认删除脚本「' + row.name + '」？将删除 ' + dirPath.value + '/' + row.name,
          '删除确认',
          { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }
        );
      } catch (e) { return; }
      try {
        const d = await api('/api/scripts/' + encodeURIComponent(row.name), { method: 'DELETE' });
        ElMessage.success(d.message || '已删除');
        if (script.value === row.name) script.value = '';
        await loadScripts();
      } catch (e) { ElMessage.error(e.message); }
    }

    function statusType(status) {
      if (status === '在线') return 'success';
      if (status === '可达(SSH未开)') return 'warning';
      if (status === '离线') return 'info';
      return 'danger';
    }

    const scriptLang = computed(() => {
      const s = scripts.value.find(x => x.name === script.value);
      return s ? s.lang : '';
    });

    function selectAll() {
      selNodes.value = nodes.value.filter(n => n.ip).map(n => n.name);
    }

    async function openLogs() {
      logsVisible.value = true;
      curSn.value = '';
      logText.value = '';
      try {
        const d = await api('/api/task-logs');
        logGroups.value = d.items || [];
        logDir.value = d.dir || '';
      } catch (e) { ElMessage.error(e.message); }
    }

    async function viewLog(sn, filename) {
      curSn.value = sn;
      loadingLog.value = true;
      try {
        const d = await api('/api/task-logs/content?sn=' + encodeURIComponent(sn)
          + '&file=' + encodeURIComponent(filename));
        logText.value = d.content || '（空文件）';
      } catch (e) {
        logText.value = '读取失败：' + e.message;
      } finally { loadingLog.value = false; }
    }

    async function run() {
      if (!script.value) { ElMessage.warning('请选择脚本'); return; }
      if (!selNodes.value.length) { ElMessage.warning('请至少选择一台节点'); return; }
      running.value = true;
      results.value = [];
      try {
        const d = await api('/api/tasks/run', {
          method: 'POST',
          body: JSON.stringify({
            script: script.value,
            nodes: selNodes.value,
            user: user.value,
            password: password.value,
            concurrency: concurrency.value,
            batch_delay: batchDelay.value,
          }),
        });
        results.value = d.items || [];
        const failed = results.value.filter(r => !r.ok).length;
        failed
          ? ElMessage.warning('执行完成：成功 ' + (results.value.length - failed) + ' 台，失败 ' + failed + ' 台')
          : ElMessage.success('执行完成：' + results.value.length + ' 台全部成功');
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        running.value = false;
      }
    }

    onMounted(loadAll);

    return {
      scripts, nodes, dirPath, script, selNodes, user, password,
      running, results, uploadVisible, file, statusType, scriptLang, selectAll,
      concurrency, batchDelay, logsVisible, logGroups, logDir, curSn, logText, loadingLog,
      loadAll, onPick, uploadScript, removeScript, run, openLogs, viewLog,
    };
  },
  template: `
    <div class="panel">
      <div class="panel-title">部署任务</div>

      <div class="task-form">
        <div class="form-row">
          <span class="form-label">执行脚本</span>
          <el-select v-model="script" placeholder="选择脚本" style="width:260px">
            <el-option v-for="s in scripts" :key="s.name" :label="s.name" :value="s.name" />
          </el-select>
          <span style="color:#909399;font-size:12px">
            {{ script ? (scriptLang === 'bash' ? 'bash -s 执行' : 'python3 - 执行') : '（从下方清单点「选用」也可）' }}
          </span>
        </div>

        <div class="form-row">
          <span class="form-label">SSH 凭据</span>
          <el-input v-model="user" placeholder="用户名" style="width:140px" />
          <el-input v-model="password" type="password" show-password placeholder="密码（留空则用服务器密钥）" style="width:220px" />
          <el-button type="primary" :loading="running"
                     :disabled="!script || !selNodes.length" @click="run">执行</el-button>
          <el-button @click="loadAll">刷新</el-button>
        </div>

        <div class="form-row">
          <span class="form-label">批量节流</span>
          <el-input-number v-model="concurrency" :min="1" :max="200" size="small" style="width:130px" />
          <span style="color:#909399;font-size:12px">台并发</span>
          <el-input-number v-model="batchDelay" :min="0" :max="3600" size="small" style="width:130px;margin-left:12px" />
          <span style="color:#909399;font-size:12px">秒 / 批间隔</span>
          <span style="color:#909399;font-size:12px;margin-left:10px">
            机器多时调小并发、加大间隔，避免几十台同时拉 ISO 把 HTTP 装机源打满
          </span>
        </div>
      </div>

      <div class="node-picker">
        <div class="picker-head">
          <b>目标节点</b>
          <el-button size="small" @click="selectAll">全选</el-button>
          <el-button size="small" @click="selNodes = []">清空</el-button>
          <span style="color:#909399;font-size:12px">已选 {{ selNodes.length }} / {{ nodes.length }} 台</span>
        </div>
        <el-checkbox-group v-model="selNodes">
          <div v-for="n in nodes" :key="n.name" class="node-item">
            <el-checkbox :label="n.name" :disabled="!n.ip">
              <span class="node-name">{{ n.name }}</span>
              <span class="text-mono node-ip">{{ n.ip || '未绑定IP' }}</span>
              <el-tag v-if="n.system" size="small" effect="plain" style="margin-left:6px">{{ n.system }}</el-tag>
              <el-tag size="small" :type="statusType(n.status)" style="margin-left:4px">{{ n.status }}</el-tag>
            </el-checkbox>
          </div>
        </el-checkbox-group>
        <div v-if="!nodes.length" style="color:#9ca3af;font-size:12px;padding:6px">
          暂无节点，请先到「节点信息」添加
        </div>
      </div>

      <el-divider content-position="left">脚本清单（{{ dirPath }}）</el-divider>
      <div class="toolbar">
        <el-button type="success" size="small" @click="uploadVisible = true">添加脚本</el-button>
        <span style="color:#909399;font-size:12px">仅识别该目录下的 .sh / .py 文件（不递归子目录）</span>
      </div>
      <el-table :data="scripts" border stripe size="small">
        <el-table-column prop="name" label="脚本名称" min-width="220" />
        <el-table-column prop="lang" label="类型" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.lang === 'bash' ? '' : 'success'">{{ row.lang }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="size" label="大小(B)" width="100" />
        <el-table-column prop="mtime" label="修改时间" width="160" />
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button size="small" @click="script = row.name">选用</el-button>
            <el-button size="small" type="danger" @click="removeScript(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty><span style="color:#909399">暂无脚本，点击「添加脚本」上传</span></template>
      </el-table>

      <el-divider content-position="left">执行结果</el-divider>
      <div class="toolbar">
        <el-button size="small" type="primary" @click="openLogs">历史执行日志</el-button>
        <span style="color:#909399;font-size:12px">
          每次执行的完整输出会自动归档到 {{ dirPath }}/log/&lt;节点SN&gt;/&lt;脚本名_时间&gt;.log
        </span>
      </div>
      <div v-if="!results.length" class="placeholder-box" style="padding:24px">尚未执行任务</div>
      <div v-for="r in results" :key="r.node" style="margin-bottom:14px">
        <div>
          <el-tag :type="r.ok ? 'success' : 'danger'" effect="dark" size="small">
            {{ r.ok ? '成功' : '失败' }}
          </el-tag>
          <b style="margin-left:8px">{{ r.node }}</b>
          <span style="color:#909399;font-size:12px;margin-left:8px">
            {{ r.host }} · 退出码 {{ r.exit_code }} · 耗时 {{ r.duration }}s
          </span>
          <el-tag v-if="r.sn" size="small" effect="plain" style="margin-left:6px">SN {{ r.sn }}</el-tag>
          <span v-if="r.log_file" class="text-mono" style="color:#909399;font-size:12px;margin-left:6px">
            📝 {{ r.log_file }}
          </span>
        </div>
        <pre class="log-box" style="height:auto;max-height:280px;margin-top:6px">{{ r.stdout || '（无标准输出）' }}</pre>
        <pre v-if="r.stderr || r.error" class="log-box"
             style="height:auto;max-height:200px;margin-top:6px;color:#ff9b9b">{{ r.error ? ('错误原因：' + r.error) : r.stderr }}</pre>
      </div>

      <el-dialog v-model="uploadVisible" title="添加脚本" width="500px">
        <el-upload action="/api/scripts" :auto-upload="false" :limit="1"
                   :on-change="onPick" :on-remove="() => file = null">
          <el-button size="small" type="primary">选择 .sh / .py 文件</el-button>
        </el-upload>
        <div style="color:#909399;font-size:12px;margin-top:8px">
          上传后保存为 {{ dirPath }}/&lt;原文件名&gt;，.sh 会自动加可执行权限。
        </div>
        <template #footer>
          <el-button @click="uploadVisible = false">取消</el-button>
          <el-button type="primary" :disabled="!file" @click="uploadScript">上传</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="logsVisible" title="历史执行日志（按节点 SN 归档）" width="980px">
        <div style="color:#909399;font-size:12px;margin-bottom:8px">归档目录：{{ logDir }}</div>
        <div style="display:flex;gap:12px;align-items:flex-start">
          <div style="flex:0 0 300px;max-height:520px;overflow:auto">
            <div v-for="g in logGroups" :key="g.sn" style="margin-bottom:12px">
              <div style="font-weight:600;margin-bottom:4px">
                {{ g.node || '（未登记）' }}
                <span class="text-mono" style="font-weight:400;color:#909399;font-size:12px">{{ g.sn }}</span>
              </div>
              <div style="color:#909399;font-size:12px;margin-bottom:4px">{{ g.ip || '-' }}</div>
              <div v-if="!g.files.length" style="color:#c0c4cc;font-size:12px">暂无日志</div>
              <div v-for="f in g.files" :key="f.name" style="margin:2px 0">
                <el-button size="small" text type="primary" @click="viewLog(g.sn, f.name)">
                  {{ f.name }}
                </el-button>
                <span style="color:#909399;font-size:11px">（{{ f.mtime }}）</span>
              </div>
            </div>
            <div v-if="!logGroups.length" style="color:#909399;font-size:12px">
              还没有归档日志，先执行一次脚本吧
            </div>
          </div>
          <div style="flex:1;min-width:0">
            <div style="color:#909399;font-size:12px;margin-bottom:6px">
              {{ curSn ? '当前：' + curSn : '点左侧文件查看内容' }}
            </div>
            <el-input v-model="logText" type="textarea" :rows="24" readonly
                      spellcheck="false" class="text-mono" v-loading="loadingLog" />
          </div>
        </div>
        <template #footer>
          <el-button @click="logsVisible = false">关闭</el-button>
        </template>
      </el-dialog>
    </div>
  `,
};

/* ------------------------------------------------------------------ 占位模块 */
const PlaceholderView = {
  props: { title: { type: String, default: '' }, desc: { type: String, default: '' } },
  template: `
    <div class="panel">
      <div class="panel-title">{{ title }}</div>
      <div class="placeholder-box">
        <div style="font-size:15px;margin-bottom:8px">{{ desc || '功能规划中…' }}</div>
        <div style="font-size:12px">（后端接口已预留，确定需求后在此接入即可）</div>
      </div>
    </div>
  `,
};

/* ------------------------------------------------------------------ 系统设置 */
const SettingsView = {
  setup() {
    const conf = ref(null);
    const out = ref('');

    async function load() {
      try { conf.value = await api('/api/settings'); } catch (e) { ElMessage.error(e.message); }
    }
    async function run(path, name) {
      try {
        const d = await api(path, { method: 'POST', body: '{}' });
        out.value = (d.ok ? '[成功] ' : '[失败] ') + (d.detail || '');
        d.ok ? ElMessage.success(name + '成功') : ElMessage.error(name + '失败');
      } catch (e) { ElMessage.error(e.message); }
    }

    const subnets = ref([]);
    const info = ref(null);
    const current = ref('');
    const form = ref(null);
    const saving = ref(false);

    function emptyForm() {
      return { subnet: '', netmask: '', range_start: '', range_end: '', routers: '', dns: '', next_server: '' };
    }

    // 不再提供"选择子网"下拉：固定读 PXE_DHCPD_CONF（默认 /etc/dhcp/dhcpd.conf），
    // 有多个 subnet 时取第一个，没有就把字段留空
    async function loadSubnets() {
      try {
        const d = await api('/api/dhcp/subnet');
        info.value = d;
        subnets.value = d.items || [];
        current.value = subnets.value.length ? subnets.value[0].subnet : '';
        fillForm();
      } catch (e) { ElMessage.error(e.message); }
    }

    function fillForm() {
      const s = subnets.value.find(x => x.subnet === current.value);
      form.value = s ? Object.assign(emptyForm(), JSON.parse(JSON.stringify(s))) : emptyForm();
    }

    async function saveSubnet() {
      if (!form.value) return;
      saving.value = true;
      try {
        const d = await api('/api/dhcp/subnet?original=' + encodeURIComponent(current.value), {
          method: 'POST',
          body: JSON.stringify(form.value),
        });
        if (d.reload_ok === false) {
          ElMessageBox.alert(
            '<pre style="white-space:pre-wrap;text-align:left;margin:0;font-size:12px">'
            + String(d.reload || '无输出').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
            + '</pre>',
            '已写入，但 dhcpd 重载失败',
            { dangerouslyUseHTMLString: true, confirmButtonText: '知道了', type: 'warning' }
          );
        } else {
          ElMessage.success(d.message || '已保存');
        }
        current.value = form.value.subnet;
        await loadSubnets();
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        saving.value = false;
      }
    }

    onMounted(async () => { await load(); await loadSubnets(); });
    return { conf, out, run, subnets, info, form, saving, saveSubnet };
  },
  template: `
    <div>
      <div class="panel">
        <div class="panel-title">
          DHCP 子网配置
          <span style="font-size:12px;font-weight:400;color:#909399;margin-left:8px">
            来源：{{ (info && info.conf) || '/etc/dhcp/dhcpd.conf' }}
          </span>
        </div>
        <div v-if="info && !subnets.length" class="settings-note">
          配置里没读到 subnet 段，以下字段留空。
        </div>
        <el-form label-width="180px" v-if="form" style="max-width:640px">
          <el-form-item label="subnet 网段">
            <el-input v-model="form.subnet" placeholder="192.168.1.0" class="text-mono" />
          </el-form-item>
          <el-form-item label="netmask 子网掩码">
            <el-input v-model="form.netmask" placeholder="255.255.255.0" class="text-mono" />
          </el-form-item>
          <el-form-item label="地址池 range">
            <el-input v-model="form.range_start" placeholder="192.168.1.100" class="text-mono" style="width:160px" />
            <span style="margin:0 8px">—</span>
            <el-input v-model="form.range_end" placeholder="192.168.1.200" class="text-mono" style="width:160px" />
          </el-form-item>
          <el-form-item label="option routers 网关">
            <el-input v-model="form.routers" placeholder="192.168.1.254" class="text-mono" />
          </el-form-item>
          <el-form-item label="name servers">
            <el-input v-model="form.dns" placeholder="8.8.8.8, 114.114.114.114" class="text-mono" />
          </el-form-item>
          <el-form-item label="next-server TFTP">
            <el-input v-model="form.next_server" placeholder="192.168.1.110" class="text-mono" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="saveSubnet">保存并重载 dhcpd</el-button>
            <span style="color:#909399;font-size:12px;margin-left:10px">
              保存前自动备份，dhcpd -t 校验失败会自动回滚
            </span>
          </el-form-item>
        </el-form>
      </div>

      <div class="panel" style="margin-top:16px">
        <div class="panel-title">环境与路径</div>
        <el-descriptions :column="1" border v-if="conf">
          <el-descriptions-item label="平台版本">{{ conf.version }}（以此确认是否部署成功）</el-descriptions-item>
          <el-descriptions-item label="节点配置目录">{{ conf.hosts_dir }}</el-descriptions-item>
          <el-descriptions-item label="匹配规则">{{ conf.hosts_glob }}</el-descriptions-item>
          <el-descriptions-item label="dhcpd 主配置">{{ conf.dhcpd_conf }}</el-descriptions-item>
          <el-descriptions-item label="iPXE 脚本">{{ conf.boot_ipxe }}</el-descriptions-item>
          <el-descriptions-item label="镜像目录">{{ conf.images_dir }}</el-descriptions-item>
          <el-descriptions-item label="SSH 默认账号">{{ conf.ssh_user }} : {{ conf.ssh_port }}</el-descriptions-item>
          <el-descriptions-item label="只读模式(DRY_RUN)">{{ conf.dry_run ? '开启（不校验不重载）' : '关闭' }}</el-descriptions-item>
        </el-descriptions>

        <div class="toolbar" style="margin-top:16px">
          <el-button type="primary" @click="run('/api/dhcp/validate','配置校验')">校验 dhcpd 配置</el-button>
          <el-button @click="run('/api/dhcp/reload','服务重载')">重载 dhcpd 服务</el-button>
        </div>

        <el-input v-if="out" type="textarea" :rows="8" :model-value="out" readonly class="text-mono" />
      </div>
    </div>
  `,
};

/* ------------------------------------------------------------------ 运行日志 */
const LogsView = {
  setup() {
    const lines = ref([]);
    const file = ref('');
    const loading = ref(false);
    const auto = ref(false);
    const el = ref(null);
    let timer = null;

    async function load() {
      loading.value = true;
      try {
        const d = await api('/api/logs?lines=300');
        lines.value = d.items || [];
        file.value = d.file || '';
        await nextTick();
        if (el.value) el.value.scrollTop = el.value.scrollHeight;
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        loading.value = false;
      }
    }

    function toggle(val) {
      if (timer) { clearInterval(timer); timer = null; }
      if (val) timer = setInterval(load, 5000);
    }

    onMounted(load);
    onBeforeUnmount(() => { if (timer) clearInterval(timer); });

    return { lines, file, loading, auto, el, load, toggle };
  },
  template: `
    <div class="panel">
      <div class="panel-title">运行日志</div>
      <div class="toolbar">
        <el-button @click="load" :loading="loading">刷新</el-button>
        <el-checkbox v-model="auto" @change="toggle">5 秒自动刷新</el-checkbox>
        <span style="color:#909399;font-size:12px">日志文件：{{ file }}</span>
      </div>
      <pre class="log-box" ref="el">{{ lines.join('\\n') }}</pre>
    </div>
  `,
};

/* ------------------------------------------------------------------ SSH 终端 */
const TerminalDialog = {
  setup() {
    const visible = ref(false);
    const creds = reactive({ user: 'root', password: '' });
    const step = ref('creds'); // creds | terminal
    const node = ref({ name: '', ip: '' });
    const termEl = ref(null);
    let term = null, fit = null, ws = null, resizeObserver = null;

    function open(row) {
      node.value = { name: row.name, ip: row.ip };
      step.value = 'creds';
      visible.value = true;
    }

    async function connect() {
      if (!creds.password) { ElMessage.warning('请输入 SSH 密码'); return; }
      step.value = 'terminal';
      await nextTick();

      term = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: 'Consolas, Menlo, monospace',
        theme: { background: '#000000' },
      });
      fit = new FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(termEl.value);
      fit.fit();
      term.writeln('\x1b[32m正在连接 ' + creds.user + '@' + node.value.ip + ' ...\x1b[0m');

      const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(scheme + '://' + location.host + '/ws/ssh?token=' + encodeURIComponent(state.token));
      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        ws.send(JSON.stringify({
          host: node.value.ip,
          user: creds.user,
          password: creds.password,
          cols: term.cols,
          rows: term.rows,
        }));
      };

      ws.onmessage = (evt) => {
        if (evt.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(evt.data));
        } else {
          try {
            const msg = JSON.parse(evt.data);
            if (msg.type === 'error') {
              term.writeln('\r\n\x1b[31m' + msg.message + '\x1b[0m');
              ElMessage.error(msg.message);
            }
          } catch (e) { /* ignore */ }
        }
      };

      ws.onclose = () => term.writeln('\r\n\x1b[90m[连接已关闭]\x1b[0m');
      ws.onerror = () => term.writeln('\r\n\x1b[31m[WebSocket 错误]\x1b[0m');

      term.onData((data) => {
        if (ws && ws.readyState === 1) ws.send(new TextEncoder().encode(data));
      });

      const doResize = () => {
        try {
          fit.fit();
          if (ws && ws.readyState === 1) {
            ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
          }
        } catch (e) { /* ignore */ }
      };
      window.addEventListener('resize', doResize);
      resizeObserver = { doResize };
    }

    function close() {
      try { if (ws) ws.close(); } catch (e) { /* ignore */ }
      try { if (term) term.dispose(); } catch (e) { /* ignore */ }
      if (resizeObserver) {
        window.removeEventListener('resize', resizeObserver.doResize);
        resizeObserver = null;
      }
      term = null; ws = null; fit = null;
      creds.password = '';
      visible.value = false;
    }

    onBeforeUnmount(close);

    return { visible, creds, step, node, termEl, open, connect, close };
  },
  template: `
    <el-dialog v-model="visible" :title="'SSH 终端 - ' + node.name + ' (' + node.ip + ')'"
               width="900px" top="5vh" @close="close" destroy-on-close>
      <div v-if="step === 'creds'">
        <el-form label-width="90px">
          <el-form-item label="登录用户"><el-input v-model="creds.user" /></el-form-item>
          <el-form-item label="密码">
            <el-input v-model="creds.password" type="password" show-password @keyup.enter="connect" />
          </el-form-item>
        </el-form>
        <div style="text-align:right">
          <el-button @click="visible = false">取消</el-button>
          <el-button type="primary" @click="connect">连接</el-button>
        </div>
      </div>
      <div v-else class="term-wrap" ref="termEl"></div>
    </el-dialog>
  `,
};

/* ------------------------------------------------------- 侧栏服务状态（WS 推送） */
/* 只给鼠标悬停提示用；侧栏上显示的文字固定是 running / dead */
const SVC_STATE_TEXT = {
  active: '运行中',
  inactive: '已停止',
  failed: '启动失败',
  activating: '启动中',
  deactivating: '停止中',
  reloading: '重载中',
  'not-found': '未安装',
  maintenance: '维护中',
  unknown: '未知',
  'dry-run': 'DRY_RUN',
};

/* 状态不走定时轮询：后端监听 systemd（dbus 事件 + 巡检兜底），只有状态变化才推过来。
   连上先收一份当前快照；断线后重连（重连的是推送通道，不是轮询状态）。
   WS 通道要是死活建不起来（反代没放行 ws、老浏览器等），自动退化为拉 HTTP 兜底。 */
const SVC_WS_RETRY_MS = 5000;
const SVC_HTTP_FALLBACK_MS = 20000;
const SVC_WS_FAIL_BEFORE_FALLBACK = 2;

const svcItems = ref([]);
let svcWs = null;
let svcRetryTimer = null;
let svcHttpTimer = null;
let svcWsTries = 0;

function svcApply(d) {
  if (d && d.items) svcItems.value = d.items;
}

function svcStopHttp() {
  if (svcHttpTimer) { clearInterval(svcHttpTimer); svcHttpTimer = null; }
}

function svcStartHttp() {
  if (svcHttpTimer) return;
  svcHttpTimer = setInterval(async () => {
    if (!state.token) return;
    try { svcApply(await api('/api/services')); } catch (e) { /* 静默，不打扰 */ }
  }, SVC_HTTP_FALLBACK_MS);
}

function svcStartWatch() {
  svcStopWatch();
  if (!state.token) { svcRetryTimer = setTimeout(svcStartWatch, 1000); return; }

  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws = null;
  try {
    ws = new WebSocket(scheme + '://' + location.host + '/ws/services?token='
      + encodeURIComponent(state.token));
  } catch (e) {
    svcWsTries += 1;
    if (svcWsTries >= SVC_WS_FAIL_BEFORE_FALLBACK) svcStartHttp();
    svcRetryTimer = setTimeout(svcStartWatch, SVC_WS_RETRY_MS);
    return;
  }

  svcWs = ws;
  ws.onopen = () => { svcWsTries = 0; svcStopHttp(); };
  ws.onmessage = (evt) => {
    try { svcApply(JSON.parse(evt.data)); } catch (e) { /* ignore */ }
  };
  ws.onclose = () => {
    if (svcWs !== ws) return;          // 已被 svcStopWatch 主动关掉
    svcWs = null;
    svcWsTries += 1;
    if (svcWsTries >= SVC_WS_FAIL_BEFORE_FALLBACK) svcStartHttp();
    if (state.token) svcRetryTimer = setTimeout(svcStartWatch, SVC_WS_RETRY_MS);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
}

function svcStopWatch() {
  if (svcRetryTimer) { clearTimeout(svcRetryTimer); svcRetryTimer = null; }
  svcStopHttp();
  svcWsTries = 0;
  if (svcWs) {
    const w = svcWs;
    svcWs = null;
    w.onclose = null;
    try { w.close(); } catch (e) { /* ignore */ }
  }
}

/* active → 绿灯 running；非 active → 红灯 dead（真实状态看鼠标悬停） */
function svcText(s) {
  return s.ok ? 'running' : 'dead';
}

function svcTip(s) {
  const label = SVC_STATE_TEXT[s.state] || s.state || '未知';
  const bits = [label + (s.sub ? ' / ' + s.sub : '')];
  if (s.pid) bits.push('PID ' + s.pid);
  if (!s.installed) bits.push('未找到该服务单元');
  if (s.detail) bits.push(s.detail);
  if (!s.ok && s.raw) bits.push('原始输出：' + s.raw);
  return bits.join('\n');
}

/* ------------------------------------------------------------------ 主应用 */
const App = {
  components: {
    LoginView, NodesView, ImagesView, TasksView, DiscoveryView,
    LogsView, SettingsView, TerminalDialog,
  },
  setup() {
    const terminal = ref(null);

    const views = {
      nodes: 'NodesView',
      images: 'ImagesView',
      tasks: 'TasksView',
      discovery: 'DiscoveryView',
      logs: 'LogsView',
      settings: 'SettingsView',
    };
    const placeholderProps = {};

    const currentView = computed(() => views[state.activeMenu] || 'NodesView');
    const currentProps = computed(() => placeholderProps[state.activeMenu] || {});

    async function checkLogin() {
      if (!state.token) return;
      try {
        const d = await api('/api/me');
        state.user = d.user;
        state.loggedIn = true;
      } catch (e) {
        logout();
      }
    }

    function onSelect(key) { state.activeMenu = key; }

    function doLogout() {
      ElMessageBox.confirm('确认退出登录？', '提示', { type: 'warning' })
        .then(() => { logout(); ElMessage.success('已退出'); })
        .catch(() => {});
    }

    onMounted(() => {
      checkLogin();
      // 暴露给子节点表格调用
      window.__openTerminal = (row) => { if (terminal.value) terminal.value.open(row); };
    });

    // 登录后挂上服务状态推送；退出/掉线时断开
    watch(() => state.loggedIn, (v) => { v ? svcStartWatch() : svcStopWatch(); }, { immediate: true });

    return { state, currentView, currentProps, onSelect, doLogout, terminal,
             svcItems, svcText, svcTip };
  },
  template: `
    <LoginView v-if="!state.loggedIn" />

    <div class="layout" v-else>
      <div class="header">
        <div class="logo">PXE 装机管理平台</div>
        <div class="right">
          <span>当前用户：{{ state.user }}</span>
          <el-button size="small" type="danger" plain @click="doLogout">退出登录</el-button>
        </div>
      </div>

      <div class="body">
        <div class="sidebar">
          <el-menu :default-active="state.activeMenu" @select="onSelect">
            <el-menu-item index="nodes">节点信息</el-menu-item>
            <el-menu-item index="images">镜像管理</el-menu-item>
            <el-menu-item index="tasks">部署任务</el-menu-item>
            <el-menu-item index="discovery">节点发现</el-menu-item>
            <el-menu-item index="logs">运行日志</el-menu-item>
            <el-menu-item index="settings">系统设置</el-menu-item>
          </el-menu>
          <!-- 服务状态：嵌在功能列表最下面，不走弹窗，状态变化由后端实时推送 -->
          <div class="svc-dock">
            <div v-for="s in svcItems" :key="s.name" class="svc-row" :title="svcTip(s)">
              <span class="svc-dot" :class="s.ok ? 'on' : 'off'"></span>
              <span class="svc-name">{{ s.name }}</span>
              <span class="svc-state" :class="s.ok ? 'ok' : 'bad'">{{ svcText(s) }}</span>
            </div>
          </div>
        </div>

        <div class="content">
          <component :is="currentView" v-bind="currentProps" />
        </div>
      </div>

      <TerminalDialog ref="terminal" />
    </div>
  `,
};

const app = createApp(App);
app.use(ElementPlus);
app.mount('#app');
