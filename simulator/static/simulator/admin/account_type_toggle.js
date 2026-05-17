/* Toggle challenge-specific admin fields based on account_type selection. */
(function () {
    'use strict';

    function applyToggle(accountType) {
        var isRetail = accountType === 'RETAIL';

        /* Hide the entire challenge/funded fieldset for retail */
        document.querySelectorAll('.challenge-section').forEach(function (el) {
            el.style.display = isRetail ? 'none' : '';
        });

        /* Highlight initial_balance label for retail — it's the key field */
        var ibLabel = document.querySelector('.field-initial_balance label');
        if (ibLabel) {
            ibLabel.style.fontWeight = isRetail ? '700' : '';
            ibLabel.style.color     = isRetail ? '#3498db' : '';
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        var select = document.getElementById('id_account_type');
        if (!select) return;

        applyToggle(select.value);
        select.addEventListener('change', function () {
            applyToggle(this.value);
        });
    });
}());
