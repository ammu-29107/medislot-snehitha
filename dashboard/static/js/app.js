document.addEventListener("DOMContentLoaded", () => {
    // Mobile navigation
    const menuToggle = document.getElementById("menuToggle");
    const sidebar = document.getElementById("sidebar");
    if (menuToggle && sidebar) {
        menuToggle.addEventListener("click", () => {
            sidebar.classList.toggle("open");
        });
    }

    // Persist the selected colour theme between pages and browser sessions.
    const themeToggle = document.getElementById("themeToggle");
    const root = document.documentElement;

    const applyTheme = (theme) => {
        const isDark = theme === "dark";
        root.setAttribute("data-theme", isDark ? "dark" : "light");
        if (themeToggle) {
            themeToggle.setAttribute("aria-pressed", String(isDark));
            themeToggle.setAttribute(
                "aria-label",
                isDark ? "Switch to light mode" : "Switch to dark mode"
            );
            const label = themeToggle.querySelector(".theme-toggle-label");
            const icon = themeToggle.querySelector(".theme-toggle-icon");
            if (label) label.textContent = isDark ? "Light mode" : "Dark mode";
            if (icon) icon.textContent = isDark ? "☀" : "◐";
        }
    };

    let savedTheme = "light";
    try {
        savedTheme = localStorage.getItem("medislot-theme") === "dark" ? "dark" : "light";
    } catch (_) {
        // The dashboard remains usable when browser storage is unavailable.
    }
    applyTheme(savedTheme);

    if (themeToggle) {
        themeToggle.addEventListener("click", () => {
            const nextTheme = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
            applyTheme(nextTheme);
            try {
                localStorage.setItem("medislot-theme", nextTheme);
            } catch (_) {
                // Theme still applies for the current page.
            }
        });
    }

    // Shared profile-photo preview modal.
    const modal = document.getElementById("imagePreviewModal");
    const modalImage = document.getElementById("imageModalPhoto");
    const modalCaption = document.getElementById("imageModalCaption");
    const modalOpenLink = document.getElementById("imageModalOpenLink");
    const modalClose = document.getElementById("imageModalClose");

    const closeImageModal = () => {
        if (!modal) return;
        modal.classList.remove("open");
        modal.setAttribute("aria-hidden", "true");
        if (modalImage) modalImage.removeAttribute("src");
    };

    const openImageModal = (source, expirySeconds) => {
        if (!modal || !modalImage || !source) return;
        modalImage.src = source;
        if (modalOpenLink) modalOpenLink.href = source;
        if (modalCaption) {
            const seconds = Number(expirySeconds);
            modalCaption.textContent = Number.isFinite(seconds) && seconds > 0
                ? `Temporary secure image link · expires in approximately ${Math.ceil(seconds / 60)} minute(s)`
                : "Profile photo";
        }
        modal.classList.add("open");
        modal.setAttribute("aria-hidden", "false");
        if (modalClose) modalClose.focus();
    };

    document.addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-image-preview]");
        if (trigger) {
            // The dashboard avatar is inside a profile link. Stop navigation
            // when the user clicks the image itself to open its preview.
            event.preventDefault();
            event.stopPropagation();
            openImageModal(
                trigger.getAttribute("data-image-preview") || trigger.currentSrc || trigger.src,
                trigger.getAttribute("data-image-expiry")
            );
        }
    });

    document.addEventListener("keydown", (event) => {
        const trigger = event.target.closest?.("[data-image-preview]");
        if (trigger && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            openImageModal(
                trigger.getAttribute("data-image-preview") || trigger.currentSrc || trigger.src,
                trigger.getAttribute("data-image-expiry")
            );
        }
        if (event.key === "Escape") closeImageModal();
    });

    if (modalClose) modalClose.addEventListener("click", closeImageModal);
    if (modal) {
        modal.addEventListener("click", (event) => {
            if (event.target === modal) closeImageModal();
        });
    }

    // Show the newly selected local photo before it is uploaded.
    const photoInput = document.getElementById("profile_photo");
    const photoPreview = document.getElementById("profilePhotoPreview");
    const photoFallback = document.getElementById("profilePhotoFallback");
    const photoOpenLink = document.getElementById("profilePhotoOpenLink");
    let localPreviewUrl = null;

    if (photoInput && photoPreview) {
        photoInput.addEventListener("change", () => {
            const file = photoInput.files && photoInput.files[0];
            if (!file) return;

            const allowedTypes = ["image/jpeg", "image/png", "image/webp"];
            if (!allowedTypes.includes(file.type)) {
                window.alert("Choose a JPEG, PNG or WEBP image.");
                photoInput.value = "";
                return;
            }
            if (file.size > 5 * 1024 * 1024) {
                window.alert("The profile photo must be 5 MB or smaller.");
                photoInput.value = "";
                return;
            }

            if (localPreviewUrl) URL.revokeObjectURL(localPreviewUrl);
            localPreviewUrl = URL.createObjectURL(file);
            photoPreview.src = localPreviewUrl;
            photoPreview.setAttribute("data-image-preview", localPreviewUrl);
            photoPreview.removeAttribute("data-image-expiry");
            photoPreview.hidden = false;
            if (photoFallback) photoFallback.hidden = true;
            if (photoOpenLink) {
                photoOpenLink.href = localPreviewUrl;
                photoOpenLink.hidden = false;
                photoOpenLink.textContent = "Open selected photo ↗";
            }
        });
    }

    // Expand/collapse consultation-type table rows.
    const toggleConsultationEditor = (summaryRow) => {
        if (!summaryRow) return;
        const targetId = summaryRow.getAttribute("data-target");
        const editorRow = targetId ? document.getElementById(targetId) : null;
        if (!editorRow) return;

        const willOpen = editorRow.hidden;
        editorRow.hidden = !willOpen;
        summaryRow.setAttribute("aria-expanded", String(willOpen));
        const toggleLabel = summaryRow.querySelector("[data-consultation-label]");
        if (toggleLabel) toggleLabel.textContent = willOpen ? "Close" : "Edit";
    };

    document.addEventListener("click", (event) => {
        const closeButton = event.target.closest("[data-consultation-close]");
        if (closeButton) {
            const editorRow = closeButton.closest(".consultation-editor-row");
            const summaryRow = editorRow?.previousElementSibling;
            if (summaryRow?.matches("[data-consultation-row]")) {
                toggleConsultationEditor(summaryRow);
            }
            return;
        }

        const summaryRow = event.target.closest("[data-consultation-row]");
        if (summaryRow) {
            // Ignore text selection and links; the summary row itself is the
            // disclosure control, and its Edit button uses the same behavior.
            if (event.target.closest("a")) return;
            toggleConsultationEditor(summaryRow);
        }
    });

    document.addEventListener("keydown", (event) => {
        const summaryRow = event.target.closest?.("[data-consultation-row]");
        if (summaryRow && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            toggleConsultationEditor(summaryRow);
        }
    });

    // Confirm destructive actions such as disabling a consultation type.
    document.addEventListener("submit", (event) => {
        const message = event.submitter?.getAttribute("data-confirm")
            || event.target.getAttribute("data-confirm");
        if (message && !window.confirm(message)) {
            event.preventDefault();
        }
    });
});
