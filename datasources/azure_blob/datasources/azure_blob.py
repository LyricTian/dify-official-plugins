from collections.abc import Generator
from typing import Dict, List, Optional, Any
import mimetypes
import os
import logging
from datetime import datetime
import requests
from azure.storage.blob import BlobServiceClient, ContainerClient
from azure.core.exceptions import AzureError, ResourceNotFoundError

# 设置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from dify_plugin.entities.datasource import (
    DatasourceMessage,
    OnlineDriveBrowseFilesRequest,
    OnlineDriveBrowseFilesResponse,
    OnlineDriveDownloadFileRequest,
    OnlineDriveFile,
    OnlineDriveFileBucket,
)
from dify_plugin.interfaces.datasource.online_drive import OnlineDriveDatasource


class AzureBlobDataSource(OnlineDriveDatasource):
    
    def invoke(self, request: Any) -> Generator[DatasourceMessage, None, None]:
        """仅使用 OnlineDrive 标准浏览/下载流程。"""
        yield from super().invoke(request)

    def _get_blob_service_client(self) -> BlobServiceClient:
        """获取 Blob 服务客户端"""
        if not hasattr(self, '_blob_service_client') or self._blob_service_client is None:
            credentials = self.runtime.credentials
            auth_method = credentials.get("auth_method", "account_key")
            account_name = credentials.get("account_name")
            endpoint_suffix = credentials.get("endpoint_suffix", "core.windows.net")
            
            if auth_method == "account_key":
                account_key = credentials.get("account_key")
                account_url = f"https://{account_name}.blob.{endpoint_suffix}"
                self._blob_service_client = BlobServiceClient(
                    account_url=account_url, 
                    credential=account_key
                )
                
            elif auth_method == "sas_token":
                sas_token = credentials.get("sas_token")
                if not sas_token.startswith('?'):
                    sas_token = '?' + sas_token
                account_url = f"https://{account_name}.blob.{endpoint_suffix}"
                self._blob_service_client = BlobServiceClient(
                    account_url=account_url + sas_token
                )
                
            elif auth_method == "connection_string":
                connection_string = credentials.get("connection_string")
                self._blob_service_client = BlobServiceClient.from_connection_string(
                    connection_string
                )
                
            elif auth_method == "oauth":
                access_token = credentials.get("access_token")
                account_url = f"https://{account_name}.blob.{endpoint_suffix}"
                
                # 创建简单的 token credential
                from azure.core.credentials import AccessToken
                from datetime import datetime, timezone
                
                class SimpleTokenCredential:
                    def __init__(self, token, expires_in=3600):
                        self.token = token
                        self.expires_at = int(datetime.now(timezone.utc).timestamp()) + expires_in
                    
                    def get_token(self, *scopes, **kwargs):
                        current_time = int(datetime.now(timezone.utc).timestamp())
                        if current_time >= self.expires_at - 300:  # 提前5分钟刷新
                            from azure.core.exceptions import ClientAuthenticationError
                            raise ClientAuthenticationError("Access token has expired, refresh required")
                        return AccessToken(self.token, self.expires_at)
                
                credential = SimpleTokenCredential(access_token)
                self._blob_service_client = BlobServiceClient(
                    account_url=account_url, 
                    credential=credential
                )
                
            else:
                raise ValueError(f"Unsupported authentication method: {auth_method}")
                
        return self._blob_service_client
    
    def _browse_files(self, request: OnlineDriveBrowseFilesRequest) -> OnlineDriveBrowseFilesResponse:
        """浏览 Azure Blob Storage 文件"""
        bucket_name = request.bucket  # 容器名
        prefix = request.prefix or ""  # Blob 前缀
        max_keys = request.max_keys or 100
        next_page_parameters = request.next_page_parameters or {}
        
        # 修复：如果 bucket_name 为空但 prefix 包含容器路径，从 prefix 中解析
        if not bucket_name and prefix:
            # 检查 prefix 是否包含容器名（格式如 "container-name/" 或 "container-name/path/"）
            prefix_parts = prefix.strip('/').split('/')
            if len(prefix_parts) >= 1:
                # 第一部分可能是容器名
                potential_container = prefix_parts[0]
                remaining_prefix = '/'.join(prefix_parts[1:]) if len(prefix_parts) > 1 else ""
                
                # 尝试验证这是否是有效的容器名
                blob_service_client = self._get_blob_service_client()
                try:
                    container_client = blob_service_client.get_container_client(potential_container)
                    # 简单检查容器是否存在（不会抛出异常说明容器存在）
                    container_client.get_container_properties()
                    
                    # 容器存在，使用解析的值
                    bucket_name = potential_container
                    prefix = remaining_prefix
                except Exception:
                    # 解析失败，继续使用原始值
                    pass
        
        try:
            blob_service_client = self._get_blob_service_client()
            
            if not bucket_name:
                # 列出所有容器
                return self._list_containers(blob_service_client, max_keys, next_page_parameters)
            else:
                # 列出指定容器中的 Blob
                return self._list_blobs_in_container(
                    blob_service_client, bucket_name, prefix, max_keys, next_page_parameters
                )
                
        except ResourceNotFoundError:
            if bucket_name:
                raise ValueError(f"Container '{bucket_name}' not found")
            else:
                raise ValueError("Storage account not accessible")
        except AzureError as e:
            raise ValueError(f"Azure Blob Storage error: {str(e)}")
        except Exception as e:
            raise ValueError(f"Failed to browse Azure Blob Storage: {str(e)}")
    
    def _list_containers(self, blob_service_client: BlobServiceClient, max_keys: int, 
                        next_page_parameters: Dict) -> OnlineDriveBrowseFilesResponse:
        """列出所有容器"""
        continuation_token = next_page_parameters.get("continuation_token")
        
        # 根据 Azure Blob SDK 文档，list_containers 不支持 results_per_page 参数
        # 使用分页迭代器来控制每页的大小
        if continuation_token:
            containers_page = blob_service_client.list_containers().by_page(
                continuation_token=continuation_token
            )
        else:
            containers_page = blob_service_client.list_containers().by_page()
        
        page = next(containers_page)
        
        files = []
        for container in page:
            # 获取容器属性
            try:
                container_client = blob_service_client.get_container_client(container.name)
                container_properties = container_client.get_container_properties()
                
                files.append(OnlineDriveFile(
                    id=container.name,
                    name=container.name,
                    size=0,  # 容器本身没有大小
                    type="folder",
                    metadata={
                        "container_name": container.name,
                        "last_modified": container.last_modified.isoformat() if container.last_modified else "",
                        "etag": container.etag or "",
                        "public_access": getattr(container_properties, "public_access", "none"),
                        "has_immutability_policy": getattr(container_properties, "has_immutability_policy", False),
                        "has_legal_hold": getattr(container_properties, "has_legal_hold", False)
                    }
                ))
            except Exception:
                # 如果无法获取容器属性，使用基本信息
                files.append(OnlineDriveFile(
                    id=container.name,
                    name=container.name,
                    size=0,
                    type="folder",
                    metadata={
                        "container_name": container.name,
                        "last_modified": container.last_modified.isoformat() if container.last_modified else "",
                        "etag": container.etag or ""
                    }
                ))
        
        # 检查是否有更多页面
        new_continuation_token = getattr(page, 'continuation_token', None)
        is_truncated = new_continuation_token is not None
        next_page_params = {"continuation_token": new_continuation_token} if is_truncated else {}
        
        return OnlineDriveBrowseFilesResponse(
            result=[OnlineDriveFileBucket(
                bucket="",  # 列出容器时 bucket 为空
                files=files,
                is_truncated=is_truncated,
                next_page_parameters=next_page_params
            )]
        )
    
    def _list_blobs_in_container(self, blob_service_client: BlobServiceClient, container_name: str,
                               prefix: str, max_keys: int, next_page_parameters: Dict) -> OnlineDriveBrowseFilesResponse:
        """列出容器中的 Blob"""
        continuation_token = next_page_parameters.get("continuation_token")
        
        try:
            container_client = blob_service_client.get_container_client(container_name)
            items_iter = container_client.walk_blobs(
                name_starts_with=prefix if prefix else None
            )

            # 分页
            items_page_iter = items_iter.by_page(continuation_token) if continuation_token else items_iter.by_page()
            page = next(items_page_iter)

            files = []
            seen_dirs = set()

            # BlobPrefix 表示目录，BlobProperties 表示文件
            for item in page:
                item_name = getattr(item, "name", None)
                if not item_name:
                    continue

                # 计算用于展示的相对名
                display_name = item_name[len(prefix):] if prefix and item_name.startswith(prefix) else item_name

                # 修正文件夹判断逻辑：只有明确的目录标记才是文件夹
                is_folder = (item_name.endswith("/") and getattr(item, "size", 0) == 0) or type(item).__name__ == "BlobPrefix"
                
                if is_folder:
                    # 目录：只展示当前层第一级目录
                    first_dir = display_name.rstrip("/")
                    if "/" in first_dir:
                        first_dir = first_dir.split("/", 1)[0]
                    
                    if first_dir and first_dir not in seen_dirs:
                        seen_dirs.add(first_dir)
                        dir_path = f"{prefix}{first_dir}/" if prefix else f"{first_dir}/"
                        # 构造正确的目录 ID 格式: container_name/dir_path
                        dir_id = f"{container_name}/{dir_path}"
                        files.append(OnlineDriveFile(
                            id=dir_id,  # 使用 container_name/dir_path 格式
                            name=first_dir,
                            size=0,
                            type="folder",
                            metadata={
                                "container_name": container_name,
                                "blob_path": dir_path,
                                "is_directory": True
                            }
                        ))
                else:
                    # 文件：仅展示当前层级（不含进一步的 /）
                    if "/" in display_name:
                        # 更深层的文件不在本层显示，由目录项承载
                        continue
                    
                    content_type = self._get_content_type(item_name, getattr(item, "content_settings", None))
                    size_val = getattr(item, "size", 0) or 0
                    last_modified = getattr(item, "last_modified", None)
                    etag = getattr(item, "etag", "") or ""
                    creation_time = getattr(item, "creation_time", None)
                    blob_tier = getattr(item, "blob_tier", "Unknown")
                    metadata_val = getattr(item, "metadata", None) or {}

                    # 构造正确的文件 ID 格式: container_name/blob_path
                    file_id = f"{container_name}/{item_name}"
                    files.append(OnlineDriveFile(
                        id=file_id,  # 使用 container_name/blob_path 格式
                        name=display_name,
                        size=size_val,
                        type="file",
                        metadata={
                            "container_name": container_name,
                            "blob_path": item_name,
                            "content_type": content_type,
                            "last_modified": last_modified.isoformat() if last_modified else "",
                            "etag": etag,
                            "blob_tier": blob_tier,
                            "creation_time": creation_time.isoformat() if creation_time else "",
                            "server_encrypted": getattr(item, "server_encrypted", False),
                            "metadata": metadata_val,
                        }
                    ))
            # 检查是否有更多页面
            new_continuation_token = getattr(page, 'continuation_token', None)
            is_truncated = new_continuation_token is not None
            next_page_params = {"continuation_token": new_continuation_token} if is_truncated else {}
            
            return OnlineDriveBrowseFilesResponse(
                result=[OnlineDriveFileBucket(
                    bucket=container_name,
                    files=files,
                    is_truncated=is_truncated,
                    next_page_parameters=next_page_params
                )]
            )
            
        except ResourceNotFoundError:
            raise ValueError(f"Container '{container_name}' not found")
        except AzureError as e:
            raise ValueError(f"Failed to list blobs in container '{container_name}': {str(e)}")
    
    def _get_content_type(self, blob_name: str, content_settings) -> str:
        """获取内容类型"""
        if content_settings and content_settings.content_type:
            return content_settings.content_type
        
        # 根据文件扩展名推断 MIME 类型
        mime_type, _ = mimetypes.guess_type(blob_name)
        return mime_type or "application/octet-stream"
    
    def _download_file(self, request: OnlineDriveDownloadFileRequest) -> Generator[DatasourceMessage, None, None]:
        """下载文件内容"""
        file_id = request.id  # 格式: container_name/blob_path
        
        if '/' not in file_id:
            raise ValueError("Invalid file ID format. Expected: container_name/blob_path")
        
        parts = file_id.split('/', 1)
        container_name = parts[0]
        blob_path = parts[1]
        
        try:
            logger.info(f"[Azure Blob] Starting download process for file: {file_id}")
            blob_service_client = self._get_blob_service_client()
            blob_client = blob_service_client.get_blob_client(
                container=container_name, 
                blob=blob_path
            )
            
            # 获取 Blob 属性
            blob_properties = blob_client.get_blob_properties()
            logger.info(f"[Azure Blob] Blob properties retrieved: size={blob_properties.size}, container={container_name}")
            
            # 检查 Blob 层级，如果是归档层需要特殊处理
            blob_tier = getattr(blob_properties, 'blob_tier', '')
            if blob_tier and blob_tier.lower() == 'archive':
                logger.error(f"[Azure Blob] Blob is in archive tier: {blob_path}")
                raise ValueError(f"Blob '{blob_path}' is in Archive tier and needs to be rehydrated before download")
            
            # 验证文件是否存在且有效
            blob_size = blob_properties.size
            if blob_size is None or blob_size < 0:
                raise ValueError(f"Invalid blob size: {blob_size}")
            
            content_type = self._get_content_type(blob_path, blob_properties.content_settings)
            logger.info(f"[Azure Blob] Blob metadata: size={blob_size}, type={content_type}, tier={blob_tier}")
            
            # 优先使用 SAS 直连 HTTP 下载（避免 SDK 受限场景）
            credentials = self.runtime.credentials
            auth_method = (credentials or {}).get("auth_method", "account_key")
            if auth_method == "sas_token":
                logger.info("[Azure Blob] Using SAS HTTP download path")
                yield from self._download_via_sas_http(container_name, blob_path)
            else:
                # 对于大文件，使用流式下载（SDK）
                if blob_size > 50 * 1024 * 1024:  # 50MB
                    logger.info(f"[Azure Blob] Using large file download for {blob_size} bytes")
                    yield from self._download_large_blob(blob_client, blob_path, content_type, blob_size)
                else:
                    logger.info(f"[Azure Blob] Using small file download for {blob_size} bytes")
                    yield from self._download_small_blob(blob_client, blob_path, content_type, blob_size)
                
            logger.info(f"[Azure Blob] Download process completed successfully for: {file_id}")
                
        except ResourceNotFoundError:
            logger.error(f"[Azure Blob] Blob not found: {blob_path} in container {container_name}")
            raise ValueError(f"Blob '{blob_path}' not found in container '{container_name}'")
        except AzureError as e:
            logger.error(f"[Azure Blob] Azure error during download: {str(e)}")
            raise ValueError(f"Failed to download blob '{blob_path}': {str(e)}")
        except Exception as e:
            logger.error(f"[Azure Blob] Unexpected error during download: {str(e)}")
            raise ValueError(f"Error downloading file: {str(e)}")

    def _download_via_sas_http(self, container_name: str, blob_path: str) -> Generator[DatasourceMessage, None, None]:
        """使用 SAS URL 通过 HTTP 下载（不依赖 SDK 的数据流）。"""
        credentials = self.runtime.credentials or {}
        account = credentials.get("account_name")
        suffix = credentials.get("endpoint_suffix", "core.windows.net")
        sas = credentials.get("sas_token") or ""
        if not account:
            raise ValueError("account_name not configured")
        if not sas:
            raise ValueError("sas_token not configured for SAS HTTP download")
        if not sas.startswith("?"):
            sas = "?" + sas
        url = f"https://{account}.blob.{suffix}/{container_name}/{blob_path}{sas}"

        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            file_name = os.path.basename(blob_path)
            content_length_header = resp.headers.get("Content-Length")
            try:
                content_length = int(content_length_header) if content_length_header else 0
            except Exception:
                content_length = 0

            # 小文件一次性返回
            if 0 < content_length <= 50 * 1024 * 1024:
                data = resp.content
                yield self.create_blob_message(data, meta={
                    "file_name": file_name,
                    "mime_type": content_type,
                    "size": len(data),
                })
                return

            # 大文件分块返回
            chunk_size = 8 * 1024 * 1024
            buffer = bytearray()
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                buffer.extend(chunk)
                if len(buffer) >= 100 * 1024 * 1024:  # 100MB 批量输出
                    yield self.create_blob_message(bytes(buffer), meta={
                        "file_name": file_name,
                        "mime_type": content_type,
                        "is_partial": True,
                    })
                    buffer = bytearray()

            if buffer:
                yield self.create_blob_message(bytes(buffer), meta={
                    "file_name": file_name,
                    "mime_type": content_type,
                    "is_partial": False,
                })
    
    def _download_small_blob(self, blob_client, blob_path: str, content_type: str, 
                           blob_size: int) -> Generator[DatasourceMessage, None, None]:
        """下载小文件"""
        try:
            logger.info(f"[Azure Blob] Starting download of small file: {blob_path}")
            download_stream = blob_client.download_blob()
            content = download_stream.readall()
            
            # 验证下载是否成功
            actual_size = len(content)
            if actual_size != blob_size:
                logger.warning(f"[Azure Blob] Size mismatch for {blob_path}: expected {blob_size}, got {actual_size}")
            else:
                logger.info(f"[Azure Blob] Successfully downloaded {blob_path}: {actual_size} bytes")
            
            # 验证内容不为空
            if not content:
                raise ValueError(f"Downloaded content is empty for blob: {blob_path}")
            
            # 提取文件名和 MIME 类型
            file_name = os.path.basename(blob_path)
            
            logger.info(f"[Azure Blob] Creating blob message for {file_name} with {actual_size} bytes")
            yield self.create_blob_message(
                blob=content,
                meta={
                    "file_name": file_name,
                    "mime_type": content_type,
                    "size": actual_size,
                    "download_success": True
                }
            )
            logger.info(f"[Azure Blob] Successfully yielded blob message for {file_name}")
            
        except Exception as e:
            raise ValueError(f"Failed to download blob content: {str(e)}")
    
    def _download_large_blob(self, blob_client, blob_path: str, content_type: str,
                           blob_size: int) -> Generator[DatasourceMessage, None, None]:
        """分块下载大文件"""
        try:
            logger.info(f"[Azure Blob] Starting download of large file: {blob_path} ({blob_size} bytes)")
            # 提取文件名
            file_name = os.path.basename(blob_path)
            
            chunk_size = 8 * 1024 * 1024  # 8MB chunks
            downloaded_content = bytearray()
            total_downloaded = 0
            
            # 分块下载
            for i in range(0, blob_size, chunk_size):
                end_range = min(i + chunk_size - 1, blob_size - 1)
                
                download_stream = blob_client.download_blob(offset=i, length=end_range - i + 1)
                chunk = download_stream.readall()
                downloaded_content.extend(chunk)
                total_downloaded += len(chunk)
                
                logger.debug(f"[Azure Blob] Downloaded chunk {i//chunk_size + 1}: {len(chunk)} bytes (total: {total_downloaded}/{blob_size})")
                
                # 如果累积的内容过大，可以分批yield
                if len(downloaded_content) > 100 * 1024 * 1024:  # 100MB
                    yield self.create_blob_message(
                        blob=bytes(downloaded_content),
                        meta={
                            "file_name": file_name,
                            "mime_type": content_type,
                            "size": len(downloaded_content),
                            "is_partial": True
                        }
                    )
                    downloaded_content = bytearray()
            
            # 验证下载完整性
            if total_downloaded != blob_size:
                logger.error(f"[Azure Blob] Download incomplete: expected {blob_size}, got {total_downloaded}")
                raise ValueError(f"Download incomplete: expected {blob_size}, got {total_downloaded}")
            
            logger.info(f"[Azure Blob] Large file download completed: {total_downloaded} bytes")
            
            # 输出剩余内容
            if downloaded_content:
                yield self.create_blob_message(
                    blob=bytes(downloaded_content),
                    meta={
                        "file_name": file_name,
                        "mime_type": content_type,
                        "size": len(downloaded_content),
                        "download_success": True,
                        "is_partial": False
                    }
                )
                
        except Exception as e:
            raise ValueError(f"Failed to download large blob: {str(e)}")
