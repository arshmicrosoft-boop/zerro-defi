// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "./interfaces/IERC20.sol";
import {
    IMorphoBlue,
    IMorphoLiquidateCallback,
    IMorphoFlashLoanCallback,
    MarketParams
} from "./interfaces/IMorphoBlue.sol";
import {ISwapRouter02} from "./interfaces/ISwapRouter.sol";

interface IDutchAuction {
    function buy(address[] calldata assets, address receiver, bytes calldata data) external returns (uint256);
}

interface IYieldStrategy {
    function harvest(address callFeeRecipient) external;
}

/// @title ZeroCapOmniExecutor
/// @notice Hardened, atomic, fail-closed execution contract for $0-capital DeFi strategies on Base.
///         Storage layout:
///           Slot 0: owner (20 bytes) + paused (1 byte)
///           Slot 1: pendingOwner (20 bytes)
///           Slot 2: allowedTargets mapping(address => bool)
///           Slot 3: _locked (0 = unlocked, 1 = locked)
contract ZeroCapOmniExecutor is IMorphoLiquidateCallback, IMorphoFlashLoanCallback {
    error Unauthorized();
    error InvalidAddress();
    error InvalidCallback();
    error InsufficientProfit(uint256 actualDelta, uint256 requiredMin);
    error ReentrancyLocked();
    error ContractPaused();
    error TargetNotAllowed(address target);
    error ArrayLengthMismatch();

    uint8 private constant CB_NONE = 0;
    uint8 private constant CB_LIQUIDATE = 1;
    uint8 private constant CB_FLASH_LOAN = 2;

    address internal constant DEFAULT_MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address internal constant DEFAULT_UNI_ROUTER = 0x2626664c2603336E57B271c5C0b26F421741e481;
    address internal constant DEFAULT_WETH = 0x4200000000000000000000000000000000000006;

    // Slot 0 (packed: owner + paused)
    address public owner;
    bool public paused;

    // Slot 1
    address public pendingOwner;

    // Slot 2
    mapping(address => bool) public allowedTargets;

    // Slot 3 (0 = unlocked, 1 = locked)
    uint256 private _locked;

    // Slot 4 & 5
    struct CallbackState {
        uint8 expectedType;
        bytes32 expectedDataHash;
    }
    CallbackState private _cbState;

    address private immutable _morpho;
    address private immutable _uniRouter;
    address private immutable _weth;

    event OwnershipTransferStarted(address indexed previousOwner, address indexed newOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event PausedStateChanged(bool isPaused);
    event TargetAllowlistUpdated(address indexed target, bool allowed);
    event MorphoLiquidationExecuted(address indexed borrower, address indexed loanToken, uint256 profitDelta);
    event DutchAuctionExecuted(address indexed auction, address indexed payToken, uint256 profitDelta);
    event HarvestBountyExecuted(address indexed strategy, uint256 wethBountyDelta);

    modifier nonReentrant() {
        if (_locked != 0) revert ReentrancyLocked();
        _locked = 1;
        _;
        _locked = 0;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    constructor(
        address _ownerAddr,
        address _morphoAddr,
        address _uniRouterAddr,
        address _wethAddr,
        address[] memory _initialTargets
    ) {
        if (
            _ownerAddr == address(0) ||
            _morphoAddr == address(0) ||
            _uniRouterAddr == address(0) ||
            _wethAddr == address(0)
        ) {
            revert InvalidAddress();
        }
        owner = _ownerAddr;
        _morpho = _morphoAddr;
        _uniRouter = _uniRouterAddr;
        _weth = _wethAddr;

        for (uint256 i = 0; i < _initialTargets.length; i++) {
            allowedTargets[_initialTargets[i]] = true;
            emit TargetAllowlistUpdated(_initialTargets[i], true);
        }
    }

    function morpho() public view returns (address) {
        return _morpho == address(0) ? DEFAULT_MORPHO : _morpho;
    }

    function uniRouter() public view returns (address) {
        return _uniRouter == address(0) ? DEFAULT_UNI_ROUTER : _uniRouter;
    }

    function weth() public view returns (address) {
        return _weth == address(0) ? DEFAULT_WETH : _weth;
    }

    // -------------------------------------------------------------------------
    // Admin & Security Controls
    // -------------------------------------------------------------------------

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert InvalidAddress();
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert Unauthorized();
        address oldOwner = owner;
        owner = pendingOwner;
        pendingOwner = address(0);
        emit OwnershipTransferred(oldOwner, owner);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit PausedStateChanged(_paused);
    }

    function setAllowedTargets(address[] calldata targets, bool allowed) external onlyOwner {
        for (uint256 i = 0; i < targets.length; i++) {
            allowedTargets[targets[i]] = allowed;
            emit TargetAllowlistUpdated(targets[i], allowed);
        }
    }

    function rescueTokens(address token, uint256 amount, address to) external onlyOwner {
        if (to == address(0)) revert InvalidAddress();
        require(IERC20(token).transfer(to, amount), "Rescue transfer failed");
    }

    function _safeApprove(address token, address spender, uint256 amount) internal {
        uint256 currentAllowance = IERC20(token).allowance(address(this), spender);
        if (currentAllowance != 0) {
            require(IERC20(token).approve(spender, 0), "Approve reset failed");
        }
        if (amount > 0) {
            require(IERC20(token).approve(spender, amount), "Approve set failed");
        }
    }

    // -------------------------------------------------------------------------
    // Pipeline A: Morpho Blue Flash Liquidation ($0 Capital)
    // -------------------------------------------------------------------------

    function executeMorphoLiquidation(
        MarketParams calldata params,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        uint24 swapFee,
        uint256 minProfit
    ) external nonReentrant whenNotPaused returns (uint256 profitDelta) {
        address m = morpho();
        uint256 balBefore = IERC20(params.loanToken).balanceOf(address(this));

        bytes memory cbData = abi.encode(params.collateralToken, params.loanToken, swapFee);
        _cbState = CallbackState({
            expectedType: CB_LIQUIDATE,
            expectedDataHash: keccak256(cbData)
        });

        IMorphoBlue(m).liquidate(
            params,
            borrower,
            seizedAssets,
            repaidShares,
            cbData
        );

        delete _cbState;

        uint256 balAfter = IERC20(params.loanToken).balanceOf(address(this));
        if (balAfter <= balBefore) revert InsufficientProfit(0, minProfit);
        profitDelta = balAfter - balBefore;
        if (profitDelta < minProfit) revert InsufficientProfit(profitDelta, minProfit);

        require(IERC20(params.loanToken).transfer(owner, profitDelta), "Profit transfer failed");
        emit MorphoLiquidationExecuted(borrower, params.loanToken, profitDelta);
    }

    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external override {
        address m = morpho();
        if (
            msg.sender != m ||
            _cbState.expectedType != CB_LIQUIDATE ||
            _cbState.expectedDataHash != keccak256(data)
        ) {
            revert InvalidCallback();
        }
        delete _cbState;

        (address collateralToken, address loanToken, uint24 swapFee) =
            abi.decode(data, (address, address, uint24));

        address router = uniRouter();
        uint256 seizedBal = IERC20(collateralToken).balanceOf(address(this));
        _safeApprove(collateralToken, router, seizedBal);

        ISwapRouter02(router).exactInputSingle(
            ISwapRouter02.ExactInputSingleParams({
                tokenIn: collateralToken,
                tokenOut: loanToken,
                fee: swapFee,
                recipient: address(this),
                amountIn: seizedBal,
                amountOutMinimum: repaidAssets,
                sqrtPriceLimitX96: 0
            })
        );

        _safeApprove(loanToken, m, repaidAssets);
    }

    // -------------------------------------------------------------------------
    // Pipeline B: Protocol Fee-Burner Dutch Auction Flash-Clear ($0 Capital)
    // -------------------------------------------------------------------------

    function executeDutchAuction(
        address auctionContract,
        address payToken,
        uint256 flashAmount,
        address[] calldata rewardTokens,
        uint256 maxAuctionPrice,
        uint24[] calldata swapFees,
        uint256[] calldata minAmountsOut,
        uint256 minProfit
    ) external nonReentrant whenNotPaused returns (uint256 profitDelta) {
        if (!allowedTargets[auctionContract]) {
            revert TargetNotAllowed(auctionContract);
        }
        if (rewardTokens.length != swapFees.length || rewardTokens.length != minAmountsOut.length) {
            revert ArrayLengthMismatch();
        }

        address m = morpho();
        uint256 balBefore = IERC20(payToken).balanceOf(address(this));

        bytes memory cbData = abi.encode(
            auctionContract,
            payToken,
            rewardTokens,
            maxAuctionPrice,
            swapFees,
            minAmountsOut
        );
        _cbState = CallbackState({
            expectedType: CB_FLASH_LOAN,
            expectedDataHash: keccak256(cbData)
        });

        IMorphoBlue(m).flashLoan(payToken, flashAmount, cbData);

        delete _cbState;

        uint256 balAfter = IERC20(payToken).balanceOf(address(this));
        if (balAfter <= balBefore) revert InsufficientProfit(0, minProfit);
        profitDelta = balAfter - balBefore;
        if (profitDelta < minProfit) revert InsufficientProfit(profitDelta, minProfit);

        require(IERC20(payToken).transfer(owner, profitDelta), "Auction profit transfer failed");
        emit DutchAuctionExecuted(auctionContract, payToken, profitDelta);
    }

    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external override {
        address m = morpho();
        if (
            msg.sender != m ||
            _cbState.expectedType != CB_FLASH_LOAN ||
            _cbState.expectedDataHash != keccak256(data)
        ) {
            revert InvalidCallback();
        }
        delete _cbState;

        (
            address auctionContract,
            address payToken,
            address[] memory rewardTokens,
            uint256 maxAuctionPrice,
            uint24[] memory swapFees,
            uint256[] memory minAmountsOut
        ) = abi.decode(data, (address, address, address[], uint256, uint24[], uint256[]));

        require(assets <= maxAuctionPrice, "Auction price exceeded max");

        _safeApprove(payToken, auctionContract, assets);
        IDutchAuction(auctionContract).buy(rewardTokens, address(this), "");

        address router = uniRouter();
        for (uint256 i = 0; i < rewardTokens.length; i++) {
            address rToken = rewardTokens[i];
            if (rToken != payToken) {
                uint256 rBal = IERC20(rToken).balanceOf(address(this));
                if (rBal > 0) {
                    _safeApprove(rToken, router, rBal);
                    ISwapRouter02(router).exactInputSingle(
                        ISwapRouter02.ExactInputSingleParams({
                            tokenIn: rToken,
                            tokenOut: payToken,
                            fee: swapFees[i],
                            recipient: address(this),
                            amountIn: rBal,
                            amountOutMinimum: minAmountsOut[i],
                            sqrtPriceLimitX96: 0
                        })
                    );
                }
            }
        }

        _safeApprove(payToken, m, assets);
    }

    // -------------------------------------------------------------------------
    // Pipeline C: Permissionless Yield Vault Harvest Bounty ($0 Capital)
    // -------------------------------------------------------------------------

    function executeHarvestBounty(address strategy, uint256 minBountyWeth)
        external
        nonReentrant
        whenNotPaused
        returns (uint256 bountyDelta)
    {
        if (!allowedTargets[strategy]) {
            revert TargetNotAllowed(strategy);
        }

        address w = weth();
        uint256 balBefore = IERC20(w).balanceOf(address(this));

        IYieldStrategy(strategy).harvest(address(this));

        uint256 balAfter = IERC20(w).balanceOf(address(this));
        if (balAfter <= balBefore) revert InsufficientProfit(0, minBountyWeth);
        bountyDelta = balAfter - balBefore;
        if (bountyDelta < minBountyWeth) revert InsufficientProfit(bountyDelta, minBountyWeth);

        require(IERC20(w).transfer(owner, bountyDelta), "Bounty transfer failed");
        emit HarvestBountyExecuted(strategy, bountyDelta);
    }
}
