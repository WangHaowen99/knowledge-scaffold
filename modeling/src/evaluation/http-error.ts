import { EvaluationAuthenticationError } from './auth-session.js';

export interface EvaluationHttpError {
  code: 'EVALUATION_AUTH_REQUIRED';
  statusCode: 401;
  message: string;
}

const AUTHENTICATION_ERROR = {
  code: 'EVALUATION_AUTH_REQUIRED',
  statusCode: 401,
  message: '登录状态已失效，请重新登录后再运行评测',
} as const;

export function mapEvaluationHttpError(error: unknown): EvaluationHttpError | null {
  return error instanceof EvaluationAuthenticationError ? AUTHENTICATION_ERROR : null;
}
