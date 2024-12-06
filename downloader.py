import os
import logging
import json
import requests
import m3u8
import subprocess
import sqlite3
import threading
import queue
import asyncio
import shutil
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request  # Agregamos Request aquí
from fastapi.requests import Request as FastAPIRequest  # Esta es la forma correcta
from pydantic import BaseModel
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import aiohttp
import asyncio
from typing import Optional, Dict, Any, List, Union

# Configuración del logging
log_file = 'video_downloader.log'
logging.basicConfig(
    handlers=[RotatingFileHandler(log_file, maxBytes=1024*1024, backupCount=5)],
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class JobStatus(str, Enum):
    """Estados posibles para los trabajos de descarga"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class Config:
    """Configuración global de la aplicación"""
    DEFAULT_CONFIG = {
        'temp_path': 'temp',
        'output_path': 'downloads',
        'default_resolution': None,
        'use_multithreading': True,
        'max_concurrent_downloads': 5,
        'max_queue_size': 100,
        'proxy': None,
        'chunk_size': 1024*1024,  # 1MB
        'max_retries': 3,
        'connection_timeout': 10,
        'ffmpeg_path': None,
        'clean_jobs_after_days': 7,
        'max_jobs_per_ip': 5
    }
    
    @classmethod
    def load_config(cls):
        """Carga la configuración desde el archivo config.json o usa los valores por defecto"""
        config_path = 'config.json'
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return {**cls.DEFAULT_CONFIG, **json.load(f)}
        else:
            # Intentar encontrar ffmpeg automáticamente
            ffmpeg_paths = [
                'ffmpeg',  # En PATH
                r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
                r'C:\ffmpeg\bin\ffmpeg.exe',
                os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ffmpeg.exe'),
                '/usr/bin/ffmpeg',
                '/usr/local/bin/ffmpeg'
            ]
            
            for path in ffmpeg_paths:
                try:
                    subprocess.run([path, '-version'], capture_output=True)
                    cls.DEFAULT_CONFIG['ffmpeg_path'] = path
                    break
                except Exception as e:
                    logger.debug(f"FFmpeg no encontrado en {path}: {str(e)}")
                    continue
            
            cls.save_config(cls.DEFAULT_CONFIG)
            return cls.DEFAULT_CONFIG
    
    @classmethod
    def save_config(cls, config):
        """Guarda la configuración en el archivo config.json"""
        with open('config.json', 'w') as f:
            json.dump(config, f, indent=4)

class DownloadRequest(BaseModel):
    """Modelo para las solicitudes de descarga"""
    url: str
    resolution: Optional[str] = None
    use_multithreading: Optional[bool] = None
    proxy: Optional[str] = None
    video_name: Optional[str] = None

class MP4DownloadRequest(BaseModel):
    """Modelo para las solicitudes de descarga directa de MP4"""
    url: str
    video_name: Optional[str] = None
    type: str = "mp4"  # Para diferenciar del endpoint m3u8

class JobResponse(BaseModel):
    """Modelo para las respuestas de estado de los trabajos"""
    job_id: str
    status: JobStatus
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    progress: Optional[float] = None
    total_segments: Optional[int] = None
    downloaded_segments: Optional[int] = None

class DatabaseManager:
    """Gestor de la base de datos SQLite para los trabajos"""
    def __init__(self, db_path: str = "jobs.db"):
        self.db_path = db_path
        self._create_tables()

    def _create_tables(self):
        """Crea las tablas necesarias en la base de datos"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    request_data TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    progress REAL,
                    total_segments INTEGER,
                    downloaded_segments INTEGER,
                    ip_address TEXT
                )
            """)
            conn.commit()

    def add_job(self, job_id: str, request_data: dict, ip_address: str) -> None:
        """Agrega un nuevo trabajo a la base de datos"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, status, created_at, request_data, 
                    progress, total_segments, downloaded_segments, ip_address
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    JobStatus.PENDING.value,
                    datetime.utcnow().isoformat(),
                    json.dumps(request_data),
                    0.0,
                    0,
                    0,
                    ip_address
                )
            )
            conn.commit()

    def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        result: Optional[dict] = None,
        error: Optional[str] = None,
        progress: Optional[float] = None,
        total_segments: Optional[int] = None,
        downloaded_segments: Optional[int] = None
    ) -> None:
        """Actualiza el estado de un trabajo"""
        updates = ["status = ?"]
        params = [status.value]

        if status == JobStatus.PROCESSING:
            updates.append("started_at = ?")
            params.append(datetime.utcnow().isoformat())
        elif status in (JobStatus.COMPLETED, JobStatus.FAILED):
            updates.append("completed_at = ?")
            params.append(datetime.utcnow().isoformat())

        if result is not None:
            updates.append("result = ?")
            params.append(json.dumps(result))
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if progress is not None:
            updates.append("progress = ?")
            params.append(progress)
        if total_segments is not None:
            updates.append("total_segments = ?")
            params.append(total_segments)
        if downloaded_segments is not None:
            updates.append("downloaded_segments = ?")
            params.append(downloaded_segments)

        params.append(job_id)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"""
                UPDATE jobs
                SET {', '.join(updates)}
                WHERE job_id = ?
                """,
                params
            )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[JobResponse]:
        """Obtiene la información de un trabajo específico"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()

            if not row:
                return None

            result = dict(row)
            if result['result']:
                result['result'] = json.loads(result['result'])
            if result['request_data']:
                result['request_data'] = json.loads(result['request_data'])
            
            return JobResponse(**result)

    def get_jobs_by_ip(self, ip_address: str, status: Optional[List[JobStatus]] = None) -> List[str]:
        """Obtiene todos los trabajos asociados a una dirección IP"""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT job_id FROM jobs WHERE ip_address = ?"
            params = [ip_address]
            
            if status:
                status_values = [s.value for s in status]
                query += f" AND status IN ({','.join('?' * len(status_values))})"
                params.extend(status_values)
            
            cursor = conn.execute(query, params)
            return [row[0] for row in cursor.fetchall()]

    def clean_old_jobs(self, days: int) -> None:
        """Limpia los trabajos más antiguos que el número de días especificado"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cutoff_date = (datetime.utcnow() - timedelta(days=days)).isoformat()
                
                # Primero, obtener los trabajos completados/fallidos a eliminar
                cursor = conn.execute(
                    """
                    SELECT job_id, result 
                    FROM jobs 
                    WHERE created_at < ? AND status IN (?, ?)
                    """,
                    (cutoff_date, JobStatus.COMPLETED.value, JobStatus.FAILED.value)
                )
                
                # Eliminar archivos de video asociados
                for row in cursor.fetchall():
                    try:
                        if row[1]:  # Si hay resultado
                            result = json.loads(row[1])
                            if 'output_path' in result:
                                output_path = Path(result['output_path'])
                                if output_path.exists():
                                    output_path.unlink()
                                    logger.info(f"Archivo eliminado: {output_path}")
                    except Exception as e:
                        logger.error(f"Error al eliminar archivo de job {row[0]}: {str(e)}")

                # Luego, eliminar los registros de la base de datos
                deleted = conn.execute(
                    """
                    DELETE FROM jobs
                    WHERE created_at < ? AND status IN (?, ?)
                    """,
                    (cutoff_date, JobStatus.COMPLETED.value, JobStatus.FAILED.value)
                ).rowcount

                conn.commit()
                logger.info(f"Se eliminaron {deleted} trabajos antiguos")
                
        except Exception as e:
            logger.error(f"Error en la limpieza de trabajos: {str(e)}")
            raise

def check_ffmpeg():
    """Verifica que ffmpeg está instalado y disponible"""
    ffmpeg_path = config.get('ffmpeg_path') or 'ffmpeg'
    logger.info(f"Verificando ffmpeg en la ruta: {ffmpeg_path}")
    
    ffmpeg_path = os.path.normpath(ffmpeg_path)
    
    try:
        use_shell = os.name == 'nt' and ffmpeg_path == 'ffmpeg'
        
        result = subprocess.run(
            [ffmpeg_path, '-version'] if not use_shell else 'ffmpeg -version',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=use_shell
        )
        
        if result.returncode != 0:
            logger.error(f"Error al ejecutar ffmpeg. Código de retorno: {result.returncode}")
            logger.error(f"Stderr: {result.stderr}")
            raise Exception(f"Error al ejecutar ffmpeg en {ffmpeg_path}")
            
        logger.info(f"FFmpeg encontrado y ejecutado exitosamente")
        logger.info(f"Versión de FFmpeg: {result.stdout.split('\n')[0]}")
        
        if ffmpeg_path != config.get('ffmpeg_path'):
            config['ffmpeg_path'] = ffmpeg_path
            Config.save_config(config)
            logger.info(f"Configuración actualizada con nueva ruta de ffmpeg: {ffmpeg_path}")
            
        return True
        
    except FileNotFoundError:
        logger.error(f"FileNotFoundError al intentar ejecutar ffmpeg")
        system_path = os.environ.get('PATH', '')
        logger.info("Variables de PATH del sistema:")
        for path in system_path.split(os.pathsep):
            logger.info(f"  - {path}")
            
        raise Exception(
            "FFmpeg no está instalado o no está en el PATH del sistema. "
            "Por favor, instala ffmpeg o especifica su ruta correcta en config.json"
        )
    except Exception as e:
        logger.error(f"Error inesperado al verificar ffmpeg: {str(e)}")
        raise

def sanitize_filename(filename: str) -> str:
    """Sanitiza el nombre del archivo para que sea seguro en el sistema de archivos"""
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    filename = filename.replace(' ', '_')
    return filename

def create_directories():
    """Crea los directorios necesarios para la aplicación"""
    Path(config['temp_path']).absolute().mkdir(parents=True, exist_ok=True)
    Path(config['output_path']).absolute().mkdir(parents=True, exist_ok=True)

def create_session():
    """Crea una sesión de requests con retry y timeouts configurados"""
    session = requests.Session()
    
    retry_strategy = Retry(
        total=config['max_retries'],
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=config['max_concurrent_downloads'],
        pool_maxsize=config['max_concurrent_downloads']
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    if config['proxy']:
        session.proxies = {
            'http': config['proxy'],
            'https': config['proxy']
        }
    
    return session

# Inicializar la aplicación FastAPI y cargar la configuración
app = FastAPI(title="Video Downloader API")
config = Config.load_config()

class VideoDownloadQueue:
    def __init__(self, max_workers: int = 3):
        self.queue = queue.Queue()
        self.db = DatabaseManager()
        self.max_workers = max_workers
        self.workers = []
        self.active = True
        self._start_workers()

    def _start_workers(self):
        """Inicia los workers para procesar la cola"""
        for _ in range(self.max_workers):
            worker = threading.Thread(target=self._worker_loop, daemon=True)
            worker.start()
            self.workers.append(worker)

    def _worker_loop(self):
        """Bucle principal del worker"""
        while self.active:
            try:
                # Intentar obtener una tarea con timeout para permitir
                # que el worker pueda terminar cuando active=False
                try:
                    job_id, request_data = self.queue.get(timeout=1)
                except queue.Empty:
                    continue

                try:
                    self.db.update_job_status(
                        job_id, 
                        JobStatus.PROCESSING,
                        progress=0.0
                    )

                    result = self._process_video_download(job_id, request_data)
                    self.db.update_job_status(
                        job_id, 
                        JobStatus.COMPLETED, 
                        result=result,
                        progress=100.0
                    )
                except Exception as e:
                    logger.error(f"Error processing job {job_id}: {str(e)}")
                    self.db.update_job_status(
                        job_id, 
                        JobStatus.FAILED, 
                        error=str(e),
                        progress=0.0
                    )
                finally:
                    # Solo marcar la tarea como completada si obtuvimos una
                    self.queue.task_done()

            except Exception as e:
                logger.error(f"Worker error: {str(e)}")

    def _select_playlist(self, m3u8_obj, requested_resolution=None):
        """Selecciona la playlist más apropiada basada en la resolución solicitada"""
        if not m3u8_obj.is_endlist and not m3u8_obj.playlists:
            return m3u8_obj
            
        playlists = sorted(
            m3u8_obj.playlists, 
            key=lambda x: (x.stream_info.resolution[0] if x.stream_info.resolution else 0), 
            reverse=True
        )
        
        if not playlists:
            raise HTTPException(status_code=400, detail="No se encontraron playlists válidas")
        
        if requested_resolution:
            requested_height = int(requested_resolution.split('x')[1])
            selected_playlist = min(
                playlists, 
                key=lambda x: abs(x.stream_info.resolution[1] - requested_height)
                if x.stream_info.resolution else float('inf')
            )
        else:
            selected_playlist = playlists[0]
        
        resolution = (
            f"{selected_playlist.stream_info.resolution[0]}x{selected_playlist.stream_info.resolution[1]}"
            if selected_playlist.stream_info.resolution 
            else "unknown"
        )
        
        return {
            'uri': selected_playlist.uri,
            'resolution': resolution,
            'bandwidth': selected_playlist.stream_info.bandwidth
        }

    def _download_segment(self, session, segment_url: str, output_path: str) -> bool:
        """Descarga un segmento individual con manejo de errores"""
        tries = 0
        max_tries = config['max_retries']
        
        while tries < max_tries:
            try:
                response = session.get(
                    segment_url, 
                    stream=True, 
                    timeout=config['connection_timeout']
                )
                response.raise_for_status()
                
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=config['chunk_size']):
                        if chunk:
                            f.write(chunk)
                return True
                
            except Exception as e:
                tries += 1
                if tries == max_tries:
                    logger.error(f"Error downloading segment after {max_tries} tries {segment_url}: {str(e)}")
                    return False
                logger.warning(f"Retry {tries}/{max_tries} for segment {segment_url}")
        
        return False

    def _combine_segments(self, temp_dir: Path, output_path: Path, resolution: Optional[str] = None) -> bool:
        """Combina los segmentos descargados en un único archivo de video"""
        try:
            logger.info(f"Combinando segmentos desde: {temp_dir}")
            logger.info(f"Archivo de salida: {output_path}")
            
            if not temp_dir.exists():
                raise Exception(f"El directorio temporal no existe: {temp_dir}")
            
            combined_ts = temp_dir / "combined.ts"
            with open(combined_ts, 'wb') as outfile:
                segment_files = sorted(
                    [f for f in temp_dir.glob("segment_*.ts")],
                    key=lambda x: int(x.stem.split('_')[1])
                )
                
                if not segment_files:
                    raise Exception(f"No se encontraron archivos de segmentos en {temp_dir}")
                
                logger.info(f"Encontrados {len(segment_files)} segmentos para combinar")
                
                total_size = 0
                for segment_file in segment_files:
                    file_size = segment_file.stat().st_size
                    total_size += file_size
                    with open(segment_file, 'rb') as infile:
                        shutil.copyfileobj(infile, outfile)
            
            logger.info(f"Segmentos combinados en archivo TS único. Tamaño total: {total_size/1024/1024:.2f} MB")
            
            ffmpeg_path = config['ffmpeg_path'] or 'ffmpeg'
            command = [
                ffmpeg_path,
                '-i', str(combined_ts),
                '-c', 'copy',  # Siempre usamos copy para mantener la calidad original
                '-movflags', '+faststart',
                '-y',
                str(output_path)
            ]
            
            logger.info(f"Ejecutando comando ffmpeg: {' '.join(command)}")
            
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            if result.returncode != 0:
                logger.error(f"Error en la salida de ffmpeg: {result.stderr}")
                raise Exception(f"Error en ffmpeg: {result.stderr}")
            
            return True
            
        finally:
            if combined_ts.exists():
                try:
                    combined_ts.unlink()
                except Exception as e:
                    logger.error(f"Error al eliminar archivo TS combinado: {str(e)}")

    def _process_video_download(self, job_id: str, request_data: dict) -> dict:
        """Procesa la descarga completa de un video"""
        session = None
        temp_dir = None
        try:
            session = create_session()
            create_directories()
            
            video_name = request_data.get('video_name') or f"video_{hash(request_data['url'])}"
            video_name = sanitize_filename(video_name)
            
            temp_dir = Path(config['temp_path']) / video_name
            output_path = Path(config['output_path']) / f"{video_name}.mp4"  # Aseguramos extensión .mp4
            
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Iniciando descarga de: {request_data['url']}")
            
            # Cargar y parsear el M3U8 maestro
            master_playlist = m3u8.load(request_data['url'])
            selected_stream = self._select_playlist(master_playlist, request_data.get('resolution'))
            
            logger.info(f"Seleccionada stream con resolución: {selected_stream['resolution']}")
            
            # Cargar la playlist de segmentos
            stream_url = selected_stream['uri']
            if not stream_url.startswith('http'):
                base_url = request_data['url'].rsplit('/', 1)[0]
                stream_url = f"{base_url}/{stream_url}"
            
            segment_playlist = m3u8.load(stream_url)
            total_segments = len(segment_playlist.segments)
            
            if not total_segments:
                raise Exception("No se encontraron segmentos en la playlist")
            
            self.db.update_job_status(
                job_id,
                JobStatus.PROCESSING,
                total_segments=total_segments,
                downloaded_segments=0
            )
            
            # Descargar segmentos
            downloaded_segments = 0
            for i, segment in enumerate(segment_playlist.segments):
                segment_path = temp_dir / f"segment_{i:05d}.ts"
                if self._download_segment(session, segment.uri, str(segment_path)):
                    downloaded_segments += 1
                    progress = (downloaded_segments / total_segments) * 100
                    self.db.update_job_status(
                        job_id,
                        JobStatus.PROCESSING,
                        progress=progress,
                        downloaded_segments=downloaded_segments
                    )
            
            if downloaded_segments < total_segments:
                raise Exception(f"Solo se descargaron {downloaded_segments} de {total_segments} segmentos")
            
            # Combinar segmentos
            if not self._combine_segments(temp_dir, output_path, request_data.get('resolution')):
                raise Exception("Error al combinar los segmentos")
            
            if not output_path.exists() or output_path.stat().st_size == 0:
                raise Exception("El archivo de salida no se creó correctamente")
            
            return {
                "status": "success",
                "output_path": str(output_path),  # Ahora retorna la ruta del MP4 final
                "resolution": selected_stream['resolution'],
                "video_name": video_name,
                "original_url": request_data['url'],
                "file_size_mb": round(output_path.stat().st_size/1024/1024, 2)
            }
            
        finally:
            if session:
                session.close()
            if temp_dir and temp_dir.exists():
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception as e:
                    logger.error(f"Error cleaning temp directory: {str(e)}")

    def add_job(self, job_id: str, request_data: dict, ip_address: str):
        """Agrega un nuevo trabajo a la cola"""
        self.db.add_job(job_id, request_data, ip_address)
        self.queue.put((job_id, request_data))

    def shutdown(self):
        """Detiene los workers y limpia recursos"""
        self.active = False
        for worker in self.workers:
            worker.join(timeout=1)

    async def _download_mp4_file(self, session: aiohttp.ClientSession, url: str, output_path: Path, job_id: str) -> bool:
        """Descarga un archivo MP4 directamente con manejo de progreso"""
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    raise HTTPException(status_code=response.status, detail=f"Error al obtener el archivo: {response.reason}")

                total_size = int(response.headers.get('content-length', 0))
                if total_size == 0:
                    raise ValueError("No se pudo determinar el tamaño del archivo")

                downloaded_size = 0
                chunk_size = config['chunk_size']

                with open(output_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            progress = (downloaded_size / total_size) * 100

                            # Actualizar progreso en la base de datos
                            self.db.update_job_status(
                                job_id,
                                JobStatus.PROCESSING,
                                progress=progress,
                                total_segments=total_size,
                                downloaded_segments=downloaded_size
                            )

                return True

        except Exception as e:
            logger.error(f"Error downloading MP4 file: {str(e)}")
            return False

    async def _process_mp4_download(self, job_id: str, request_data: dict) -> dict:
        """Procesa la descarga directa de un archivo MP4"""
        try:
            create_directories()
            
            video_name = request_data.get('video_name') or f"video_{hash(request_data['url'])}"
            video_name = sanitize_filename(video_name)
            
            output_path = Path(config['output_path']) / f"{video_name}.mp4"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Iniciando descarga MP4 de: {request_data['url']}")
            
            # Verificar que la URL sea válida
            parsed_url = urlparse(request_data['url'])
            if not all([parsed_url.scheme, parsed_url.netloc]):
                raise ValueError("URL inválida")

            self.db.update_job_status(
                job_id,
                JobStatus.PROCESSING,
                progress=0.0
            )

            # Crear sesión aiohttp con retry
            timeout = aiohttp.ClientTimeout(total=None, connect=config['connection_timeout'])
            async with aiohttp.ClientSession(timeout=timeout) as session:
                success = await self._download_mp4_file(session, request_data['url'], output_path, job_id)

            if not success or not output_path.exists() or output_path.stat().st_size == 0:
                raise Exception("Error en la descarga del archivo MP4")

            return {
                "status": "success",
                "output_path": str(output_path),
                "video_name": video_name,
                "original_url": request_data['url'],
                "file_size_mb": round(output_path.stat().st_size/1024/1024, 2)
            }

        except Exception as e:
            logger.error(f"Error en la descarga MP4: {str(e)}")
            raise

    def add_mp4_job(self, job_id: str, request_data: dict, ip_address: str):
        """Agrega un nuevo trabajo de descarga MP4 a la cola"""
        self.db.add_job(job_id, request_data, ip_address)
        asyncio.create_task(self._handle_mp4_job(job_id, request_data))

    async def _handle_mp4_job(self, job_id: str, request_data: dict):
        """Maneja el proceso de descarga MP4 de manera asíncrona"""
        try:
            result = await self._process_mp4_download(job_id, request_data)
            self.db.update_job_status(
                job_id, 
                JobStatus.COMPLETED, 
                result=result,
                progress=100.0
            )
        except Exception as e:
            logger.error(f"Error processing MP4 job {job_id}: {str(e)}")
            self.db.update_job_status(
                job_id, 
                JobStatus.FAILED, 
                error=str(e),
                progress=0.0
            )

# Inicializar la cola global
download_queue = VideoDownloadQueue(max_workers=config['max_concurrent_downloads'])

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        check_ffmpeg()
        logger.info("FFmpeg verificado y disponible")

        # Iniciar tarea de limpieza
        async def cleanup_old_jobs():
            while True:
                try:
                    download_queue.db.clean_old_jobs(config['clean_jobs_after_days'])
                    await asyncio.sleep(24 * 60 * 60)  # Ejecutar cada 24 horas
                except Exception as e:
                    logger.error(f"Error en la limpieza de trabajos: {str(e)}")
                    await asyncio.sleep(60)  # Esperar un minuto antes de reintentar

        # Crear la tarea de limpieza
        cleanup_task = asyncio.create_task(cleanup_old_jobs())

    except Exception as e:
        logger.error(f"Error al verificar FFmpeg: {str(e)}")
        raise

    yield

    # Shutdown
    download_queue.shutdown()
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

# Eliminar el decorador @app.on_event ya que ahora está en el lifespan
app = FastAPI(lifespan=lifespan)

@app.post("/download/", response_model=JobResponse)
async def start_download(
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
    client_request: Request  # Agregamos esto para obtener la IP
):
    """Inicia una nueva descarga de video"""
    # Verificar límite de trabajos por IP
    client_ip = client_request.client.host
    active_jobs = download_queue.db.get_jobs_by_ip(
        client_ip,
        status=[JobStatus.PENDING, JobStatus.PROCESSING]
    )
    
    if len(active_jobs) >= config['max_jobs_per_ip']:
        raise HTTPException(
            status_code=429,
            detail=f"Máximo de {config['max_jobs_per_ip']} trabajos activos permitidos por IP"
        )
    
    job_id = f"job_{hash(request.url)}_{int(datetime.utcnow().timestamp())}"
    request_data = request.dict()
    
    download_queue.add_job(job_id, request_data, client_ip)
    
    return JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=datetime.utcnow().isoformat()
    )

@app.post("/download/mp4", response_model=JobResponse)
async def start_mp4_download(
    request: MP4DownloadRequest,
    background_tasks: BackgroundTasks,
    client_request: Request
):
    """Inicia una nueva descarga directa de archivo MP4"""
    client_ip = client_request.client.host
    active_jobs = download_queue.db.get_jobs_by_ip(
        client_ip,
        status=[JobStatus.PENDING, JobStatus.PROCESSING]
    )
    
    if len(active_jobs) >= config['max_jobs_per_ip']:
        raise HTTPException(
            status_code=429,
            detail=f"Máximo de {config['max_jobs_per_ip']} trabajos activos permitidos por IP"
        )
    
    job_id = f"mp4_job_{hash(request.url)}_{int(datetime.utcnow().timestamp())}"
    request_data = request.dict()
    
    download_queue.add_mp4_job(job_id, request_data, client_ip)
    
    return JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=datetime.utcnow().isoformat()
    )

@app.get("/job/{job_id}", response_model=JobResponse)
async def get_job_status(job_id: str):
    """Obtiene el estado actual de un trabajo"""
    job = download_queue.db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    return job

@app.delete("/job/{job_id}")
async def cancel_job(job_id: str):
    """Cancela un trabajo en curso"""
    job = download_queue.db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    
    if job.status in [JobStatus.COMPLETED, JobStatus.FAILED]:
        raise HTTPException(status_code=400, detail="No se puede cancelar un trabajo ya finalizado")
    
    download_queue.db.update_job_status(job_id, JobStatus.FAILED, error="Trabajo cancelado por el usuario")
    return {"status": "success", "message": "Trabajo cancelado exitosamente"}

async def start_cleanup_task():
    """Inicia la tarea de limpieza periódica de trabajos antiguos"""
    async def cleanup_old_jobs():
        while True:
            try:
                download_queue.db.clean_old_jobs(config['clean_jobs_after_days'])
                await asyncio.sleep(24 * 60 * 60)  # Ejecutar cada 24 horas
            except Exception as e:
                logger.error(f"Error en la limpieza de trabajos: {str(e)}")
                await asyncio.sleep(60)  # Esperar un minuto antes de reintentar

    asyncio.create_task(cleanup_old_jobs())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)