-- Additive Jishiyun extension for an EXISTING MyEMS installation.
-- BACK UP myems_system_db and myems_historical_db before applying.
-- Does not create/drop databases, rewrite existing data, or change tbl_versions.
-- Re-running creates missing tables and registers the protocol; it does not migrate incompatible tables.

INSERT INTO `myems_system_db`.`tbl_protocols` (`name`, `code`)
SELECT 'Jishiyun', 'JISHIYUN'
WHERE NOT EXISTS (
  SELECT 1 FROM `myems_system_db`.`tbl_protocols` WHERE `code` = 'JISHIYUN'
);

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_backfill_batches` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `request_key` CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `data_source_id` BIGINT NOT NULL,
  `task_id` BIGINT NOT NULL,
  `requested_by_user_id` BIGINT NOT NULL,
  `window_start_utc` DATETIME NOT NULL,
  `window_end_utc` DATETIME NOT NULL,
  `child_count` INT UNSIGNED NOT NULL,
  `is_cancelled` BOOL NOT NULL DEFAULT FALSE,
  `cancelled_by_user_id` BIGINT NULL,
  `cancelled_datetime_utc` DATETIME NULL,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tbl_jishiyun_backfill_batches_unique_1` (`request_key`),
  INDEX `tbl_jishiyun_backfill_batches_index_1` (`data_source_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_business_jobs` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `data_source_id` BIGINT NOT NULL,
  `run_id` BIGINT NOT NULL,
  `actor_user_id` BIGINT NOT NULL,
  `status` ENUM('queued', 'running', 'blocked', 'done', 'error') NOT NULL DEFAULT 'queued',
  `attempt_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `gate_code` VARCHAR(128) NULL,
  `lease_token` CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  `lease_expires_datetime_utc` DATETIME NULL,
  `next_attempt_datetime_utc` DATETIME NULL,
  `register_insert_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `hour_insert_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tbl_jishiyun_business_jobs_unique_1` (`run_id`),
  INDEX `tbl_jishiyun_business_jobs_index_1` (`data_source_id`, `status`, `next_attempt_datetime_utc`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_tasks` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `data_source_id` BIGINT NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `task_type` ENUM('archive_sync', 'meter_initialize', 'energy_collect', 'energy_backfill', 'recalculate') NOT NULL,
  `is_enabled` BOOL NOT NULL DEFAULT FALSE,
  `schedule_minute` TINYINT UNSIGNED NOT NULL DEFAULT 40,
  `delay_minutes` SMALLINT UNSIGNED NOT NULL DEFAULT 40,
  `lookback_hours` SMALLINT UNSIGNED NOT NULL DEFAULT 3,
  `provider_utc_offset_minutes` SMALLINT NOT NULL,
  `parameters_json` LONGTEXT NOT NULL COMMENT 'Configuration only; never store API keys',
  `created_by_user_id` BIGINT NOT NULL,
  `updated_by_user_id` BIGINT NOT NULL,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `tbl_jishiyun_tasks_index_1` (`data_source_id`, `task_type`, `is_enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_runs` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `task_id` BIGINT NOT NULL,
  `data_source_id` BIGINT NOT NULL,
  `request_key` CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `status` ENUM('preview', 'queued', 'running', 'partial', 'done', 'error', 'cancelled') NOT NULL,
  `window_start_utc` DATETIME NULL,
  `window_end_utc` DATETIME NULL COMMENT 'Exclusive end for coverage; differencing boundary fetched separately',
  `scope_snapshot_json` LONGTEXT NOT NULL COMMENT 'Expected devices and effective scope; not inferred from API response',
  `parameters_json` LONGTEXT NOT NULL COMMENT 'Approved initialization settings or collection parameters; no secrets',
  `requested_by_user_id` BIGINT NULL,
  `requested_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `started_datetime_utc` DATETIME NULL,
  `finished_datetime_utc` DATETIME NULL,
  `attempt_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `request_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `next_attempt_datetime_utc` DATETIME NULL,
  `lease_token` CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  `lease_expires_datetime_utc` DATETIME NULL,
  `expected_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `returned_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `valid_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `inserted_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `updated_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `duplicate_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `invalid_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `missing_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `unexpected_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `error_code` VARCHAR(64) NULL,
  `error_message` TEXT NULL COMMENT 'Sanitized; no headers, credentials or response bodies',
  PRIMARY KEY (`id`),
  UNIQUE KEY `tbl_jishiyun_runs_unique_1` (`request_key`),
  INDEX `tbl_jishiyun_runs_index_1` (`status`, `next_attempt_datetime_utc`, `lease_expires_datetime_utc`),
  INDEX `tbl_jishiyun_runs_index_2` (`task_id`, `requested_datetime_utc`),
  INDEX `tbl_jishiyun_runs_index_3` (`data_source_id`, `window_start_utc`, `window_end_utc`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_run_items` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `run_id` BIGINT NOT NULL,
  `external_address` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `external_id` BIGINT NOT NULL,
  `archive_json` LONGTEXT NOT NULL,
  `approved_configuration_json` LONGTEXT NULL,
  `status` ENUM('preview', 'pending', 'created', 'reused', 'conflict', 'error') NOT NULL,
  `meter_id` BIGINT NULL,
  `point_id` BIGINT NULL,
  `error_message` TEXT NULL,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tbl_jishiyun_run_items_unique_1` (`run_id`, `external_address`),
  UNIQUE KEY `tbl_jishiyun_run_items_unique_2` (`run_id`, `external_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_meter_mappings` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `data_source_id` BIGINT NOT NULL,
  `external_id` BIGINT NOT NULL,
  `external_address` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `meter_id` BIGINT NOT NULL,
  `point_id` BIGINT NOT NULL,
  `initialization_run_id` BIGINT NOT NULL,
  `archive_json` LONGTEXT NOT NULL,
  `configuration_status` ENUM('pending', 'ready', 'conflict') NOT NULL DEFAULT 'pending',
  `is_enabled` BOOL NOT NULL DEFAULT FALSE,
  `collection_start_utc` DATETIME NULL,
  `collection_end_utc` DATETIME NULL,
  `last_archive_sync_datetime_utc` DATETIME NOT NULL,
  `last_complete_datetime_utc` DATETIME NULL,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tbl_jishiyun_meter_mappings_unique_1` (`data_source_id`, `external_address`),
  UNIQUE KEY `tbl_jishiyun_meter_mappings_unique_2` (`data_source_id`, `external_id`),
  UNIQUE KEY `tbl_jishiyun_meter_mappings_unique_3` (`point_id`),
  UNIQUE KEY `tbl_jishiyun_meter_mappings_unique_4` (`meter_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_alerts` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `data_source_id` BIGINT NOT NULL,
  `external_address` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `reading_datetime_utc` DATETIME NOT NULL,
  `alert_type` ENUM('missing', 'invalid', 'conflict', 'boundary_missing', 'identity_mismatch') NOT NULL,
  `status` ENUM('active', 'recovered') NOT NULL DEFAULT 'active',
  `first_run_id` BIGINT NOT NULL,
  `last_run_id` BIGINT NOT NULL,
  `first_seen_datetime_utc` DATETIME NOT NULL,
  `last_seen_datetime_utc` DATETIME NOT NULL,
  `recovered_datetime_utc` DATETIME NULL,
  `acknowledged_by_user_id` BIGINT NULL,
  `acknowledged_datetime_utc` DATETIME NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tbl_jishiyun_alerts_unique_1`
    (`data_source_id`, `external_address`, `reading_datetime_utc`, `alert_type`),
  INDEX `tbl_jishiyun_alerts_index_1` (`data_source_id`, `status`, `reading_datetime_utc`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_events` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `data_source_id` BIGINT NOT NULL,
  `run_id` BIGINT NULL,
  `mapping_id` BIGINT NULL,
  `alert_id` BIGINT NULL,
  `actor_user_id` BIGINT NULL COMMENT 'NULL for worker events',
  `event_type` VARCHAR(64) NOT NULL,
  `details_json` LONGTEXT NOT NULL COMMENT 'Before/after configuration, alert lifecycle, sanitized errors; no secrets',
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  INDEX `tbl_jishiyun_events_index_1` (`data_source_id`, `created_datetime_utc`),
  INDEX `tbl_jishiyun_events_index_2` (`run_id`, `id`),
  INDEX `tbl_jishiyun_events_index_3` (`alert_id`, `id`),
  INDEX `tbl_jishiyun_events_index_4` (`mapping_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_worker_state` (
  `data_source_id` BIGINT NOT NULL,
  `worker_id` CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `heartbeat_datetime_utc` DATETIME NOT NULL,
  `lease_expires_datetime_utc` DATETIME NOT NULL,
  `next_request_datetime_utc` DATETIME NOT NULL COMMENT 'Compatibility heartbeat field; credential quota is in tbl_jishiyun_request_quotas',
  PRIMARY KEY (`data_source_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_system_db`.`tbl_jishiyun_request_quotas` (
  `credential_fingerprint` CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT 'SHA-256 credential identifier; never raw API key',
  `next_request_datetime_utc` DATETIME NOT NULL,
  `is_blocked` BOOL NOT NULL DEFAULT FALSE COMMENT 'Malformed provider cooldown: manual review/reset required',
  PRIMARY KEY (`credential_fingerprint`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `myems_historical_db`.`tbl_jishiyun_energy_readings` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `data_source_id` BIGINT NOT NULL,
  `external_address` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `external_id` BIGINT NOT NULL,
  `reading_datetime_utc` DATETIME NOT NULL,
  `date_type` ENUM('mi15') NOT NULL DEFAULT 'mi15',
  `value_type` ENUM('SJZ') NOT NULL DEFAULT 'SJZ',
  `forward_active_register` DECIMAL(21,6) NULL COMMENT 'ZYGDNSZ only; NULL for invalid or missing field, never substitute zero',
  `quality` ENUM('valid', 'invalid', 'conflict') NOT NULL,
  `raw_record_json` LONGTEXT NOT NULL,
  `first_run_id` BIGINT NOT NULL,
  `last_run_id` BIGINT NOT NULL,
  `revision` INT UNSIGNED NOT NULL DEFAULT 1,
  `created_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_datetime_utc` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `tbl_jishiyun_energy_readings_unique_1`
    (`data_source_id`, `external_address`, `reading_datetime_utc`, `date_type`, `value_type`),
  INDEX `tbl_jishiyun_energy_readings_index_1` (`reading_datetime_utc`),
  INDEX `tbl_jishiyun_energy_readings_index_2` (`last_run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
