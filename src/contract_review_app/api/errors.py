"""错误响应"""

from __future__ import annotations

import uuid
from typing import Optional


ERROR_CODE_MESSAGES = {
    "FailedOperation.DownLoadError": "文件下载失败。",
    "FailedOperation.ImageDecodeFailed": "图片解码失败。",
    "FailedOperation.ImageNoText": "图片中未检测到文本。",
    "FailedOperation.ContractReviewTaskFailed": "异步合同审查任务执行失败。",
    "FailedOperation.UnKnowError": "未知错误。",
    "FailedOperation.UnOpenError": "服务不可用。",
    "InvalidParameterValue.InvalidParameterValueLimit": "参数值错误。",
    "LimitExceeded.TooLargeFileError": "文件内容过大。",
    "FailedOperation.CardSideError": "身份证 CardSide 类型错误。",
    "FailedOperation.ClassifyStoreFailed": "图片分类失败。",
    "FailedOperation.ImageBlur": "图片模糊。",
    "FailedOperation.ImageNoBusinessCard": "图片未检测到名片。",
    "FailedOperation.ImageNoHandWrite": "图片中未检测到手写体。",
    "FailedOperation.ImageNoIdCard": "图片中未检测到身份证。",
    "FailedOperation.ImageNoSpecifiedCard": "非指定卡类别图片。",
    "FailedOperation.ImageSizeTooLarge": "图片尺寸过大。",
    "FailedOperation.NoBizLicense": "非营业执照。",
    "InvalidParameter.EngineImageDecodeFailed": "图片解码失败。",
    "AuthFailure.SignatureFailure": "认证失败，请检查签名或 Token。",
    "AuthFailure.InvalidToken": "缺少认证 Token，请使用 X-API-Token 头提供。",
    "ResourceNotFound.TaskNotFound": "任务不存在。",
    "FailedOperation.ContractReviewFailed": "合同审查失败。",
    "FailedOperation.ContractPreviewFailed": "打开合同原文失败。",
    "FailedOperation.ContractCompareFailed": "合同文档对比失败。",
    "FailedOperation.TaskNotCompleted": "任务尚未完成。",
    "FailedOperation.TaskFailed": "任务执行失败。",
    "FailedOperation.TaskExpired": "任务结果已过期。",
    "LimitExceeded.QueueFull": "任务队列已满，请稍后重试。",
    "Conflict.ReviewResultChanged": "审查结果已变化或不是服务器当前版本，请重新获取后重试。",
    "InvalidParameterValue.InvalidTaskType": "不支持的任务类型。",
}


def get_message_for_code(code: str) -> str:
    """返回错误码对应的默认提示文案。"""

    return ERROR_CODE_MESSAGES.get(code, "未知错误。")


def infer_error_code_from_detail(status_code: int, detail: str) -> tuple[str, str]:
    """根据状态码和错误详情推断统一错误码。"""

    text = (detail or "").strip()
    # 优先匹配现有中文错误短语
    if status_code == 401:
        if "Token" in text or "缺少" in text:
            return "AuthFailure.InvalidToken", get_message_for_code("AuthFailure.InvalidToken")
        return "AuthFailure.SignatureFailure", get_message_for_code("AuthFailure.SignatureFailure")
    if status_code == 404 and ("任务不存在" in text or "任务" in text):
        return "ResourceNotFound.TaskNotFound", text or get_message_for_code("ResourceNotFound.TaskNotFound")
    if status_code == 409 and ("队列已满" in text or "队列" in text):
        return "LimitExceeded.QueueFull", text or get_message_for_code("LimitExceeded.QueueFull")
    if status_code == 503:
        return "FailedOperation.UnOpenError", text or get_message_for_code("FailedOperation.UnOpenError")
    if "文件下载失败" in text or "下载失败" in text:
        return "FailedOperation.DownLoadError", text or get_message_for_code("FailedOperation.DownLoadError")
    if "图片解码失败" in text or "Base64 解码失败" in text or "解码失败" in text:
        return "FailedOperation.ImageDecodeFailed", text or get_message_for_code("FailedOperation.ImageDecodeFailed")
    if "未检测到文本" in text:
        return "FailedOperation.ImageNoText", text or get_message_for_code("FailedOperation.ImageNoText")
    if "过大" in text:
        return "LimitExceeded.TooLargeFileError", text or get_message_for_code("LimitExceeded.TooLargeFileError")
    if "任务尚未完成" in text:
        return "FailedOperation.TaskNotCompleted", text or get_message_for_code("FailedOperation.TaskNotCompleted")
    if "任务结果已过期" in text:
        return "FailedOperation.TaskExpired", text or get_message_for_code("FailedOperation.TaskExpired")
    if "任务执行失败" in text:
        return "FailedOperation.TaskFailed", text or get_message_for_code("FailedOperation.TaskFailed")
    if status_code >= 500:
        return "FailedOperation.UnKnowError", text or get_message_for_code("FailedOperation.UnKnowError")
    return "InvalidParameterValue.InvalidParameterValueLimit", text or get_message_for_code(
        "InvalidParameterValue.InvalidParameterValueLimit"
    )


def make_error_response(code: str, message: Optional[str] = None, request_id: Optional[str] = None) -> dict:
    """构造兼容现有腾讯云风格的错误响应体。"""

    return {
        "Response": {
            "Error": {
                "Code": code,
                "Message": message or get_message_for_code(code),
            },
            "RequestId": request_id or str(uuid.uuid4()),
        }
    }


class AppError(Exception):
    """携带 HTTP 状态码与业务错误码的领域异常。"""

    def __init__(
        self,
        status_code: int = 500,
        error_code: str = "FailedOperation.UnKnowError",
        message: Optional[str] = None,
    ):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message or get_message_for_code(error_code)
        super().__init__(self.message)
