# GABE M3U8 Video Downloader API

## Overview

The Video Downloader API is a robust FastAPI-based service that enables downloading videos from M3U8 streams and direct MP4 sources. It provides a queue-based system with progress tracking, job management, and automatic cleanup features.

## Key Features

- HLS (M3U8) video stream downloading with quality selection
- Direct MP4 file downloading
- Automatic quality selection based on resolution
- Progress tracking and status updates
- Job queue management with concurrent downloads
- IP-based rate limiting
- Automatic cleanup of old jobs
- FFmpeg integration for video processing
- Configurable settings via JSON
- Comprehensive error handling and logging

## Installation Requirements

### System Requirements
- Python 3.7+
- FFmpeg installed and accessible
- Sufficient storage space for temporary and output files

### Required Python Packages
```
fastapi
aiohttp
m3u8
requests
uvicorn
pydantic
tqdm
```

## Configuration

The application uses a `config.json` file with the following settings:

```json
{
    "temp_path": "temp",
    "output_path": "downloads",
    "default_resolution": null,
    "use_multithreading": true,
    "max_concurrent_downloads": 5,
    "max_queue_size": 100,
    "proxy": null,
    "chunk_size": 1048576,
    "max_retries": 3,
    "connection_timeout": 10,
    "ffmpeg_path": null,
    "clean_jobs_after_days": 7,
    "max_jobs_per_ip": 5
}
```

## API Endpoints

### 1. Start M3U8 Download
```http
POST /download/
```

Request body:
```json
{
    "url": "string",
    "resolution": "string" (optional),
    "use_multithreading": boolean (optional),
    "proxy": "string" (optional),
    "video_name": "string" (optional)
}
```

### 2. Start MP4 Download
```http
POST /download/mp4
```

Request body:
```json
{
    "url": "string",
    "video_name": "string" (optional),
    "type": "mp4"
}
```

### 3. Get Job Status
```http
GET /job/{job_id}
```

### 4. Cancel Job
```http
DELETE /job/{job_id}
```

## Job States

- `PENDING`: Job is queued but not yet started
- `PROCESSING`: Download is in progress
- `COMPLETED`: Download finished successfully
- `FAILED`: Download encountered an error

## Job Response Format

```json
{
    "job_id": "string",
    "status": "JobStatus",
    "created_at": "string",
    "started_at": "string",
    "completed_at": "string",
    "result": {
        "status": "string",
        "output_path": "string",
        "resolution": "string",
        "video_name": "string",
        "original_url": "string",
        "file_size_mb": number
    },
    "error": "string",
    "progress": number,
    "total_segments": number,
    "downloaded_segments": number
}
```

## Error Handling

The API implements several layers of error handling:

1. **Network Errors**: Automatic retries with exponential backoff
2. **Invalid URLs**: Validation before processing
3. **Resource Limits**: IP-based rate limiting
4. **File System Errors**: Automatic cleanup of temporary files
5. **FFmpeg Errors**: Detailed error logging and reporting

## Database Management

The application uses SQLite for job tracking with the following features:

- Persistent job status storage
- Progress tracking
- Automatic cleanup of old jobs
- IP-based job limiting

## Security Features

1. **Input Validation**: All inputs are validated using Pydantic models
2. **Rate Limiting**: IP-based job limits
3. **Path Sanitization**: File names are sanitized to prevent path traversal
4. **Proxy Support**: Optional proxy configuration for security
5. **Timeout Controls**: Configurable connection timeouts

## Performance Optimization

1. **Chunked Downloads**: Large files are processed in chunks
2. **Concurrent Processing**: Multiple worker threads for parallel downloads
3. **Connection Pooling**: Reuse of HTTP connections
4. **Efficient File Handling**: Streaming response handling
5. **Memory Management**: Temporary file cleanup

## Best Practices for Usage

1. **Resolution Selection**:
   - Let the API auto-select quality if unsure
   - Specify resolution only when needed
   - Format: "1920x1080"

2. **Job Management**:
   - Monitor job status regularly
   - Clean up completed jobs
   - Handle failed jobs appropriately

3. **Resource Management**:
   - Set appropriate concurrent download limits
   - Monitor disk space usage
   - Configure cleanup intervals

4. **Error Handling**:
   - Implement proper retry logic
   - Log errors for debugging
   - Handle timeout scenarios

## Logging

The application uses a rotating file logger with the following configuration:

- Log file: 'video_downloader.log'
- Max file size: 1MB
- Backup count: 5 files
- Log level: INFO

## Development and Extension

The codebase is designed to be modular and extensible:

1. **Adding New Features**:
   - Implement new endpoint handlers
   - Extend the database schema
   - Add new download processors

2. **Custom Processing**:
   - Modify segment processing
   - Implement custom FFmpeg commands
   - Add new output formats

3. **Monitoring**:
   - Add metrics collection
   - Implement health checks
   - Enhanced logging

## Common Issues and Solutions

1. **FFmpeg Not Found**:
   - Ensure FFmpeg is installed
   - Set correct path in config.json
   - Check system PATH

2. **Download Failures**:
   - Check network connectivity
   - Verify URL accessibility
   - Check disk space
   - Review log files

3. **Performance Issues**:
   - Adjust concurrent download limits
   - Monitor system resources
   - Check network bandwidth

## Contributing

When contributing to this project:

1. Follow the existing code style
2. Add appropriate error handling
3. Update documentation
4. Add unit tests
5. Test thoroughly before submitting