-- ---------------------------------------------------------------------------------------------------------------------
-- WARNING: BACKUP YOUR DATABASE BEFORE UPGRADING
-- THIS SCRIPT UPGRADES 6.3.13 TO 6.3.14
-- ---------------------------------------------------------------------------------------------------------------------

START TRANSACTION;

UPDATE `myems_system_db`.`tbl_menus`
SET `parent_menu_id` = NULL
WHERE `route` = '/ai-assistant';

UPDATE `myems_system_db`.`tbl_versions`
SET version='6.3.14', release_date='2026-10-07'
WHERE id=1;

COMMIT;