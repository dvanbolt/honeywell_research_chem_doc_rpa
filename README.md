# Honeywell Document Bot

An automated solution for retrieving Certificate of Analysis (CoA) and Certificate of Origin (CoO) documents from 
Honeywell's Research Chemicals public portal. Not intended for wide-spread adoption but also not proprietary in any 
way so here it is.  It's a very thin python orchestration layer which gathers a reference list of product/batch combinations 
from a CSV or Azure SQL source and then attempts to download them from the portal with Selenium and keep a cached catalog of the 
product/batch/file associations as well as the number of attempts made to find a document for each.

## Features

- Automated document retrieval from Honeywell's Research Chemicals doc portal
- Support for both CoA and CoO documents
- Flexible scheduling with cron-based timing
- Multiple data source options (SQL Server and CSV)
- Local caching system for tracking document retrieval status
- Configurable logging system with console and file output
- Azure AD authentication support for SQL connections

## Prerequisites

- Python 3.11-3.13
- Chrome WebDriver
- Azure CLI (if you want to get the batch information from D365 F&O or other Azure source)
- Required Python packages (see `requirements.txt`):
  - selenium
  - pyodbc
  - azure-identity
  - croniter
  - pyyaml
  - colorama
  - zoneinfo

## Installation

1. Clone the repository:
```bash
git clone [repository-url]
cd [repository-name]
```

2. Install required packages:
```bash
pip install -r requirements.txt
```

3. Create a configuration file `config.yaml` in the project directory (see Configuration section)

## Configuration

The bot is configured using a YAML file. Here's a sample configuration structure:

```yaml
default_config: "default"
configs:
  default:
    document: "coa"  # or "coo"
    document_path: "path/to/save/documents"
    
    batch_source:
      type: "SQLBatchSource"  # or "CSVBatchSource"
      server: "your-server"
      database: "your-database"
      tenant_id: "your-azure-tenant-id"
      #file_path: "path to static csv of [item_number],[catalog_number],[batch_number]
    cache:
      type: "LocalCache"
      path: "path/to/cache.pkl"
      
    schedule:
      enabled: true
      cron: "0 * * * * America/New_York"
      polling_interval: 60 #seconds after which to check if job needs to run"
      
    log:
      console: true
      console_level: "INFO"
      file: true
      file_level: "DEBUG"
      file_path: "logs/bot.log"
      
    run:
      refresh_cache: true
      save_cache: true
      print_cache: false
      max_attempts: 3 #number of attempts to try to fetch document over the life the cache unless it is dropped
      limit: 0 #limit number of records to search for.  For testing
```

## Usage

### Basic Usage

Run the bot once:
```python
from main import HoneywellCoXDocumentBot

bot = HoneywellCoXDocumentBot()
bot.run_once()
```

### Run as a Service

Run the bot as a scheduled service:
```python
bot = HoneywellCoXDocumentBot()
bot.run(service=True)
```

### Custom Configuration Path

Specify a custom configuration path:
```python
bot = HoneywellCoXDocumentBot("path/to/config/directory")
```

## Data Sources

### SQL Server Source
Uses Azure AD authentication to connect to SQL Server and retrieve batch information.

### CSV Source
Reads batch information from a CSV file with required columns:
- item_number
- catalog_number
- batch_number

## Caching

The bot maintains a cache of processed batches to track:
- Number of retrieval attempts
- Last attempt timestamp
- Document file path (if successfully retrieved)

Cache can be implemented either as:
- LocalCache: File-based pickle storage
- SQLCache: Database storage (to be implemented)

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE) file for details.
