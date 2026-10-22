import type { RemediationSummary } from '../api/types';

export const remediationLabels: Record<string, string> = {
  pending: '待审批', approved: '已批准', rejected: '已拒绝', executing: '执行中 · 等待结果',
  succeeded: '流程已完成', failed: '动作失败', verification_failed: '独立验证未通过', closed: '人工已收口',
  passed: '业务验证通过', no_data: '验证无数据', timeout: '验证超时', stale: '证据已过期',
  mismatch: '证据或目标不匹配', unknown: '结果未知 · 需要核查',
  not_executed: '确认未执行', executed: '确认已执行', blocked: '补偿被阻断',
  approval_required: '可申请 · 仍需独立审批', already_requested: '已有补偿提案',
  bound: '已绑定本次动作', legacy: '旧记录缺少绑定', manual_observation: '人工当前观测',
};
export const compensationReasons: Record<string, string> = {
  safe_compensation_target_not_registered: '没有注册经过验证的安全补偿目标。不会重开故障开关或使用监控配置快照回滚。',
  independent_approval_required: '具备候选补偿契约，须独立人工审批并在执行前重新校验目标。',
  compensation_already_exists: '已经存在关联补偿提案，请在该提案中继续处理。',
  original_not_executed: '原动作确认未执行，无需创建反向副作用。',
  original_execution_unknown: '原动作结果未知，需要核查和人工处置，不能盲目补偿。',
  original_state_not_eligible: '原提案状态不允许补偿申请。',
  original_action_failed: '原动作未明确成功，缺少可补偿的确定状态。',
  original_binding_mismatch: '原审批、参数或执行记录不匹配。',
  verification_not_confirmed: '缺少唯一、完整的独立动作后验证。',
  verification_binding_mismatch: '验证与动作、服务、版本或参数摘要不匹配。',
  verification_no_data: '独立验证无数据，不能据此认定业务失败。',
  verification_timeout: '独立验证超时，不能据此认定业务失败。',
  verification_unknown: '旧记录或当前证据没有明确的业务验证结局。',
  verification_stale: '验证证据已过期，需要可信的重新评估。',
  verification_mismatch: '验证目标或证据不匹配。',
  compensation_recursion_forbidden: '补偿提案不再自动派生下一轮补偿，需要人工处置。',
  conflicting_operation: '同一服务存在冲突操作，请先处理当前提案。',
  conflicting_unknown_operation: '同一服务存在结果未知的历史动作，需要人工核查。',
};
export const reasonLabel = (reason: string) => compensationReasons[reason] || `操作被阻断（${reason}）`;
export function remediationState(item: RemediationSummary) {
  if (['executing', 'failed', 'closed'].includes(item.status) && item.execution_outcome === 'unknown') return remediationLabels.unknown;
  if (item.original_proposal_id && item.status === 'failed') return '补偿动作失败';
  if (item.original_proposal_id && item.status === 'verification_failed') return '补偿后独立验证未通过';
  return remediationLabels[item.status];
}
