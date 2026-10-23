<template>
  <div style="height: 100%; overflow-y: auto; padding-right: 4px">
    <div
      style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 8px">
      <div style="display: flex; align-items: center; gap: 16px; flex-wrap: wrap">
        <span style="font-size: 16px; font-weight: 500">跨账号转存任务管理</span>
      </div>
      <a-button type="primary" @click="openCreateModal">
        <template #icon>
          <PlusOutlined />
        </template>
        新建跨账号转存任务
      </a-button>
    </div>
    <a-table 
      :columns="columns"
      :data-source="tasks"
      :loading="tableLoading"
      size="small"
      :pagination="{ defaultValue: 1, pageSize: 10, showSizeChanger: true }">
      <template #bodyCell="{ column, record }"> 
        <template v-if="column.key === 'enabled'">
          <a-switch v-model:checked="record.enabled" @change="toggleTransferTaskEnabled(record.id)" />
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space>
            <a-button type="link" @click="editTransferTask(record)">编辑</a-button>
            <a-button type="link" @click="() => deleteTransferTask(record.id)">删除</a-button>
            <a-button type="link" @click="() => triggerTransferTask(record.id)">立即执行</a-button>
          </a-space>
        </template>
        <template v-else>
          {{ record[column.dataIndex] }}
        </template>
      </template>
    </a-table>
    <a-modal v-model:open="modalVisible" 
    width="600px" 
    ok-text="保存"
    :title="editingTaskId ? '编辑跨账号转存任务' : '新建跨账号转存任务'" 
    :modalSubmitting="modalSubmitting" 
    :maskClosable="false" 
    :destroyOnClose="true"
    @ok="handleModalOK">
      <a-form :model="formState" :rules="formRules" ref="formRef" :label-col="{ span: 6 }" :wrapper-col="{ span: 16 }" layout="vertical">
        <a-form-item label="任务名称" name="task_name" :rules="[{ required: true, message: '请输入任务名称' }]">
          <a-input v-model:value="formState.task_name" placeholder="请输入任务名称" />
        </a-form-item>

        <a-form-item label="源账号" name="source_account" :rules="[{ required: true, message: '请选择源账号' }]">
          <a-select v-model:value="formState.source_account" placeholder="请选择源账号">
            <a-select-option v-for="account in accounts" :key="account.id" :value="account.id">
              {{ account.name }}
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="目标账号" name="target_account" :rules="[{ required: true, message: '请选择目标账号' }]">
          <a-select v-model:value="formState.target_account" placeholder="请选择目标账号">
            <a-select-option v-for="account in accounts" :key="account.id" :value="account.id">
              {{ account.name }}
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="源文件夹" name="source_folder" :rules="[{ required: true, message: '请输入源文件夹路径' }]">
          <a-input v-model:value="formState.source_folder" placeholder="请输入源文件夹路径" />
        </a-form-item>
        <a-form-item label="目标文件夹" name="target_folder" :rules="[{ required: true, message: '请输入目标文件夹路径' }]">
          <a-input v-model:value="formState.target_folder" placeholder="请输入目标文件夹路径" />
        </a-form-item>
        <a-form-item label="定时计划" name="schedule_cron" :rules="[{ required: true, message: '请选择定时计划' }]">
          <a-input v-model:value="formState.schedule_cron" placeholder="请选择定时计划">
          </a-input>
          <div style="font-size: 11px; color: #888; margin-top: 4px">
            五域格式：分 时 日 月 周。可参考：<code>*/5 * * * *</code> (每5分钟)，<code>0 */2 * * *</code> (每2小时)
          </div>
        </a-form-item>
        <a-form-item label="转存后删除源文件">
          <a-switch v-model:checked="formState.on_remove_file_after_transfer" />
          <div style="font-size: 12px; color: #ff4d4f; margin-top: 4px">
            提示：开启此选项后，转存成功的文件将从源账号中删除，请谨慎使用。
          </div>
        </a-form-item>
        <a-form-item label="转存后分享文件">
          <a-switch v-model:checked="formState.on_share_file_after_transfer" />
          <div style="font-size: 12px; color: #888; margin-top: 4px">
            提示：开启此选项后，转存成功的文件将自动分享。
          </div>
        </a-form-item>
        <a-form-item label="分享后删除文件">
          <a-switch v-model:checked="formState.on_remove_file_after_share" />
          <div style="font-size: 12px; color: #ff4d4f; margin-top: 4px">
            提示：开启此选项后，创建分享链接成功后文件将从目标账号中删除，请谨慎使用。
          </div>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">

import { ref, reactive, onMounted } from 'vue';
import axios from 'axios';
import { message } from 'ant-design-vue';
import { PlusOutlined } from '@ant-design/icons-vue';

interface Account {
  id: number;
  name: string;
  enabled: boolean;
}

interface TransferTask {
  id: number;
  name: string;
  source_account: number;
  target_account: number;
  source_folder: string;
  target_folder: string;
  schedule_cron: string;
  enabled: boolean;
  on_remove_file_after_transfer: boolean;
  on_share_file_after_transfer: boolean;
  on_remove_file_after_share: boolean;
}

interface TransferTaskApiResponse {
  id: number;
  name: string;
  source_account_id: number;
  target_account_id: number;
  source_dir_path: string;
  target_dir_path: string;
  cron_expression: string;
  enabled: boolean;
  remove_source_after_transfer: boolean;
  share_file_after_transfer: boolean;
  remove_file_after_share: boolean;
}

interface TransferTaskForm {
  task_name: string;
  source_account: number | string;
  target_account: number | string;
  source_folder: string;
  target_folder: string;
  schedule_cron: string;
  on_remove_file_after_transfer: boolean;
  on_share_file_after_transfer: boolean;
  on_remove_file_after_share: boolean;
}

const toApiTask = (form: TransferTaskForm) => ({
  name: form.task_name,
  source_account_id: Number(form.source_account),
  target_account_id: Number(form.target_account),
  source_dir_path: form.source_folder,
  target_dir_path: form.target_folder,
  cron_expression: form.schedule_cron,
  remove_source_after_transfer: form.on_remove_file_after_transfer,
  share_file_after_transfer: form.on_share_file_after_transfer,
  remove_file_after_share: form.on_remove_file_after_share,
});

const fromApiTask = (task: TransferTaskApiResponse): TransferTask => ({
  id: task.id,
  name: task.name,
  source_account: task.source_account_id,
  target_account: task.target_account_id,
  source_folder: task.source_dir_path,
  target_folder: task.target_dir_path,
  schedule_cron: task.cron_expression,
  enabled: task.enabled,
  on_remove_file_after_transfer: task.remove_source_after_transfer,
  on_share_file_after_transfer: task.share_file_after_transfer,
  on_remove_file_after_share: task.remove_file_after_share,
});

// State for modal visibility and form data
const tasks = ref<TransferTask[]>([]);
const editingTaskId = ref<number | null>(null);
const modalVisible = ref(false);
const formState = reactive({
  task_name: 'Test',
  source_account: '',
  target_account: '',
  source_folder: '/妙传发送',
  target_folder: '/秒传接收',
  schedule_cron: '0 */8 * * *',
  on_remove_file_after_transfer: false, // 是否在转存后删除源文件
  on_share_file_after_transfer: false, // 是否在转存后分享文件
  on_remove_file_after_share: false, // 是否在分享后删除文件
});
const formRules = {
  task_name: [{ required: true, message: '请输入任务名称', trigger: 'blur' }],
  source_account: [{ required: true, message: '请选择源账号', trigger: 'change' }],
  target_account: [{ required: true, message: '请选择目标账号', trigger: 'change' }],
  source_folder: [{ required: true, message: '请输入源文件夹路径', trigger: 'blur' }],
  target_folder: [{ required: true, message: '请输入目标文件夹路径', trigger: 'blur' }],
  schedule_cron: [{ required: true, message: '请输入定时计划', trigger: 'blur' }],
  checkAccounts: [
    {
      validator: (_rule: any, _value: any, callback: any) => {
        if (formState.source_account === formState.target_account) {
          callback(new Error('源账号和目标账号不能相同'));
        } else {
          callback();
        }
      },
      trigger: 'change',
    },
  ],
};
const columns = [
  { title: '任务名称', key: 'name', dataIndex: 'name' },
  { title: '源账号', key: 'source_account', dataIndex: 'source_account' },
  { title: '目标账号', key: 'target_account', dataIndex: 'target_account' },
  { title: '源文件夹', key: 'source_folder', dataIndex: 'source_folder' },
  { title: '目标文件夹', key: 'target_folder', dataIndex: 'target_folder' },
  { title: '定时计划', key: 'schedule_cron', dataIndex: 'schedule_cron' },
  { title: '启用状态', key: 'enabled', dataIndex: 'enabled' },
  { title: '操作', key: 'actions' },
];
const formRef = ref();
const accounts = ref<Account[]>([]);
const tableLoading = ref(false);
const modalSubmitting = ref(false);

// Function to open the modal for creating a new transfer task

const handleModalOK = () => {
  if (editingTaskId.value) {
    updateTransferTask(editingTaskId.value);
  } else {
    createTransferTask();
  }
};
const openCreateModal = () => {
  // Logic to open the modal for creating a new transfer task
  console.log('Open create transfer task modal');
  modalVisible.value = true;
};
const editTransferTask = (task: TransferTask) => {
  editingTaskId.value = task.id;
  Object.assign(formState, {
    task_name: task.name,
    source_account: task.source_account,
    target_account: task.target_account,
    source_folder: task.source_folder,
    target_folder: task.target_folder,
    schedule_cron: task.schedule_cron,
    on_remove_file_after_transfer: task.on_remove_file_after_transfer,
    on_share_file_after_transfer: task.on_share_file_after_transfer,
    on_remove_file_after_share: task.on_remove_file_after_share,
  });
  modalVisible.value = true;
};
const loadAccounts = async () => {
  try {
    const res = await axios.get('/api/accounts/');
    accounts.value = res.data.accounts || [];
  } catch (e) {
    console.error(e);
    message.error('加载账号列表失败，请检查网络连接或稍后重试');
  }
};
const fetchTransferTasks = async () => {
  try {
    tableLoading.value = true;
    const res = await axios.get('/api/scheduled-transfer/');
    console.log('Fetched transfer tasks:', res.data);
    tasks.value = (res.data.tasks || []).map(fromApiTask);
  } catch (e) {
    console.error(e);
    message.error('加载转存任务列表失败，请检查网络连接或稍后重试');
  } finally {
    tableLoading.value = false;
  }
};
const createTransferTask = async () => {
  try {
    await formRef.value.validate();
    modalSubmitting.value = true;
    console.log('Creating transfer task with data:', toApiTask(formState));
    const res = await axios.post('/api/scheduled-transfer/create', toApiTask(formState));
    console.log('Transfer task created:', res.data);
    modalVisible.value = false;
    await fetchTransferTasks();
  } catch (e) {
    console.error(e);
    message.error('创建转存任务失败，请检查网络连接或稍后重试');
  } finally {
    modalSubmitting.value = false;
  }
};
const updateTransferTask = async (taskId: number) => {
  try {
    await formRef.value.validate();
    modalSubmitting.value = true;
    const res = await axios.put(`/api/scheduled-transfer/${taskId}`, toApiTask(formState));
    console.log('Transfer task updated:', res.data);
    modalVisible.value = false;
    await fetchTransferTasks();
  } catch (e) {
    console.error(e);
    message.error('更新转存任务失败，请检查网络连接或稍后重试');
  } finally {
    modalSubmitting.value = false;
  }
};
const deleteTransferTask = async (taskId: number) => {
  try {
    const res = await axios.delete(`/api/scheduled-transfer/${taskId}/`);
    console.log('Transfer task deleted:', res.data);
    await fetchTransferTasks();
  } catch (e) {
    console.error(e);
    message.error('删除转存任务失败，请检查网络连接或稍后重试');
  }
};
const triggerTransferTask = async (taskId: number) => {
  try {
    const res = await axios.post(`/api/scheduled-transfer/${taskId}/run`);
    console.log('Transfer task triggered:', res.data);
    // Optionally, refresh the list of transfer tasks here
    await fetchTransferTasks();
  } catch (e) {
    console.error(e);
    message.error('触发转存任务失败，请检查网络连接或稍后重试');
  }
};
const toggleTransferTaskEnabled = async (taskId: number) => {
  try {
    const res = await axios.post(`/api/scheduled-transfer/${taskId}/toggle`);
    console.log('Transfer task enabled state updated:', res.data);
    // Optionally, refresh the list of transfer tasks here
    await fetchTransferTasks();
  } catch (e) {
    console.error(e);
    message.error('更新转存任务状态失败，请检查网络连接或稍后重试');
  }
};

onMounted(() => {
  loadAccounts();
  fetchTransferTasks();
});

</script>

<style scoped></style>