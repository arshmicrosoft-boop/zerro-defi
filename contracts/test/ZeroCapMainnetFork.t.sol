// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ZeroCapOmniExecutor} from "../src/ZeroCapOmniExecutor.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";

interface ISimpleAccountFactory {
    function getAddress(address owner, uint256 salt) external view returns (address);
}

interface IWETH is IERC20 {
    function deposit() external payable;
}

contract LiveForkDutchAuction {
    address public immutable payToken;
    address public immutable rewardToken;
    uint256 public immutable price;

    constructor(address _payToken, address _rewardToken, uint256 _price) {
        payToken = _payToken;
        rewardToken = _rewardToken;
        price = _price;
    }

    function buy(address[] calldata, address receiver, bytes calldata) external returns (uint256) {
        require(IERC20(payToken).transferFrom(msg.sender, address(this), price), "pay transferFrom failed");
        uint256 rBal = IERC20(rewardToken).balanceOf(address(this));
        require(IERC20(rewardToken).transfer(receiver, rBal), "reward transfer failed");
        return price;
    }
}

/// @title ZeroCapMainnetForkTest
/// @notice Executes live Base Mainnet fork integration tests against real Morpho Blue,
///         real Uniswap V3 SwapRouter02 (WETH/USDC 500bps pool), and real ERC-4337 SimpleAccountFactory.
contract ZeroCapMainnetForkTest {
    address constant MORPHO_BLUE = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address constant UNI_ROUTER = 0x2626664c2603336E57B271c5C0b26F421741e481;
    address constant WETH_BASE = 0x4200000000000000000000000000000000000006;
    address constant USDC_BASE = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant SIMPLE_ACCOUNT_FACTORY = 0x9406Cc6185a346906296840746125a0E44976454;

    /// @notice Proves live Base Mainnet Morpho Blue 0.00%-fee flashLoan + live Uniswap V3 WETH->USDC swap + profit transfer.
    function testLiveMorphoFlashLoanAndUniswapV3Swap() public payable {
        address operatorOwner = ISimpleAccountFactory(SIMPLE_ACCOUNT_FACTORY).getAddress(address(0xABCD), 0);
        require(operatorOwner != address(0), "SimpleAccountFactory getAddress failed");

        IWETH(WETH_BASE).deposit{value: 0.01 ether}();

        LiveForkDutchAuction liveAuction = new LiveForkDutchAuction(
            USDC_BASE,
            WETH_BASE,
            10_000_000
        );
        require(IERC20(WETH_BASE).transfer(address(liveAuction), 0.01 ether), "Seed WETH failed");

        address[] memory targets = new address[](1);
        targets[0] = address(liveAuction);

        ZeroCapOmniExecutor executor = new ZeroCapOmniExecutor(
            operatorOwner,
            MORPHO_BLUE,
            UNI_ROUTER,
            WETH_BASE,
            targets
        );

        address[] memory rewards = new address[](1);
        rewards[0] = WETH_BASE;
        uint24[] memory fees = new uint24[](1);
        fees[0] = 500;
        uint256[] memory minOuts = new uint256[](1);
        minOuts[0] = 11_000_000;

        uint256 ownerUsdcBefore = IERC20(USDC_BASE).balanceOf(operatorOwner);
        uint256 profitDelta = executor.executeDutchAuction(
            address(liveAuction),
            USDC_BASE,
            10_000_000,
            rewards,
            10_000_000,
            fees,
            minOuts,
            1_000_000
        );

        uint256 ownerUsdcAfter = IERC20(USDC_BASE).balanceOf(operatorOwner);
        require(profitDelta >= 1_000_000, "Live fork profit below 1 USDC");
        require(ownerUsdcAfter - ownerUsdcBefore == profitDelta, "Owner USDC delta mismatch");
    }

    function runAllForkTests() external payable returns (uint256 passedCount) {
        testLiveMorphoFlashLoanAndUniswapV3Swap();
        passedCount++;
    }
}
