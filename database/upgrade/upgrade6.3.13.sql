-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.12 TO 6.3.13
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

ALTER TABLE `myems_system_db`.`tbl_ai_model_configs`
  MODIFY COLUMN `api_key_env_name` VARCHAR(128) NULL,
  ADD COLUMN `api_key_ciphertext` TEXT NULL AFTER `api_key_env_name`;

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.13', release_date='2026-10-07'
WHERE id=1;

COMMIT;